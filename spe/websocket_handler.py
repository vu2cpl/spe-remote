"""Multi-client WebSocket handler for SPE amplifier remote control."""

import json
import logging
import time
from typing import Set

import tornado.websocket

from spe.protocol import AmplifierState

logger = logging.getLogger(__name__)


class AmplifierWebSocket(tornado.websocket.WebSocketHandler):
    """WebSocket handler supporting multiple simultaneous clients."""

    clients: Set["AmplifierWebSocket"] = set()
    _serial_handler = None
    _power_controller = None
    _tune_orchestrator = None
    _radio_controller = None
    _app_config = None
    _config_path = "config.yaml"
    _last_json = ""
    _last_broadcast_time = 0.0
    _heartbeat_interval = 15.0

    @classmethod
    def configure(cls, serial_handler, power_controller=None,
                  tune_orchestrator=None, radio_controller=None,
                  app_config=None, config_path="config.yaml",
                  heartbeat: float = 15.0) -> None:
        cls._serial_handler = serial_handler
        cls._power_controller = power_controller
        cls._tune_orchestrator = tune_orchestrator
        cls._radio_controller = radio_controller
        cls._app_config = app_config
        cls._config_path = config_path
        cls._heartbeat_interval = heartbeat

    def check_origin(self, origin) -> bool:
        return True

    def open(self) -> None:
        AmplifierWebSocket.clients.add(self)
        logger.info(
            f"Client connected ({self.request.remote_ip}), "
            f"{len(self.clients)} total"
        )
        # Send current state immediately to new client
        if self._serial_handler:
            state_json = self._serial_handler.state.to_json()
            try:
                self.write_message(state_json)
            except tornado.websocket.WebSocketClosedError:
                pass

    def on_message(self, message: str) -> None:
        # Strip surrounding whitespace before dispatching. Various
        # WS clients (websocat with --read-mode=lines, browser
        # dashboards, Node-RED) sometimes include a trailing \n or
        # \r\n which would otherwise break an exact-string compare
        # below. Log the raw message (repr) at INFO so we can see
        # exactly what arrived if dispatching ever gets weird.
        raw = message
        message = message.strip()
        logger.info(
            f"Command from {self.request.remote_ip}: {message!r}"
            + (f" (raw={raw!r})" if raw != message else "")
        )
        if message == "power_on" and self._power_controller:
            # Power ON requires DTR hardware toggle (no serial command exists)
            import tornado.ioloop
            tornado.ioloop.IOLoop.current().spawn_callback(
                self._handle_power, message
            )
        elif message == "power_off" and self._power_controller:
            # Power OFF uses serial command 0x0A (SWITCH OFF)
            import tornado.ioloop
            tornado.ioloop.IOLoop.current().spawn_callback(
                self._handle_power, message
            )
        elif message == "tune_single" and self._tune_orchestrator:
            # Single-freq ATU tune cycle. Runs as a background task so
            # the WS handler doesn't block other clients during the
            # ~3-5 s cycle. Status updates broadcast as `tune_event`
            # JSON messages — see _broadcast_tune_event() below.
            import tornado.ioloop
            tornado.ioloop.IOLoop.current().spawn_callback(
                self._tune_orchestrator.tune_single
            )
        elif message.startswith("tune_band:") and self._tune_orchestrator:
            # Sweep the SPE manual's recommended sub-bands. THE RADIO
            # RULES THE BAND: the orchestrator derives the band from
            # the radio's slice freq and sweeps that, overriding the
            # payload band with a note if it disagrees (the antenna
            # follows the radio, so the radio's band is the safe one).
            # The payload band (tune_band:20m, case-insensitive; also
            # tune_band:auto / "" / "current") is only trusted when
            # the radio's band can't be read. The amp is dropped to
            # STBY for the sweep and OPERATE is restored at the end
            # iff it was on at the start. Antenna selection stays with
            # the operator.
            band = message.split(":", 1)[1].strip()
            import tornado.ioloop
            tornado.ioloop.IOLoop.current().spawn_callback(
                self._tune_orchestrator.tune_band, band
            )
        elif message == "tune_stop" and self._tune_orchestrator:
            # Abort an in-progress cycle (single or sweep). The
            # orchestrator's finally block guarantees the carrier is
            # cut before it returns. Sweep checks the stop event
            # before each sub-band so abort lands quickly.
            self._tune_orchestrator.stop()
        elif message in ("radio_connect", "flex_connect"):
            # Sent when a client opens its Sweep menu — pre-warm the radio
            # connection so it's ready when the operator hits Start.
            # Idempotent; RadioController broadcasts RADIO_CONNECTING /
            # RADIO_CONNECTED / RADIO_ERROR so the UI can reflect status.
            # No-op when no radio is configured — handled here (not
            # forwarded to the serial handler as an amp command).
            # `flex_connect` is kept as an alias for older clients.
            if self._radio_controller:
                import tornado.ioloop
                tornado.ioloop.IOLoop.current().spawn_callback(
                    self._radio_controller.connect
                )
        elif message in ("radio_disconnect", "flex_disconnect"):
            # Sent when a client closes its Sweep menu while idle. Don't
            # drop the radio mid-tune — the orchestrator owns the
            # connection for the duration of a cycle and disconnects
            # itself when the cycle is over.
            if self._radio_controller and not (
                self._tune_orchestrator and self._tune_orchestrator.is_running
            ):
                import tornado.ioloop
                tornado.ioloop.IOLoop.current().spawn_callback(
                    self._radio_controller.disconnect
                )
        elif message == "get_config":
            # Reply (to this client only) with the current radio config so
            # the client can render its radio picker / settings form.
            self._send_radio_config()
        elif message.startswith("set_radio_config:") and self._radio_controller:
            # Client-driven radio selection / settings edit. Payload is
            # JSON, e.g. {"kind":"tci","tci":{"host":"127.0.0.1","port":50001}}.
            # Applies live (no restart) and persists to config.yaml.
            import tornado.ioloop
            payload = message.split(":", 1)[1]
            tornado.ioloop.IOLoop.current().spawn_callback(
                self._handle_set_radio_config, payload
            )
        elif message.startswith("set_temp_unit:") and self._serial_handler:
            # Live temperature-unit toggle. Example payloads: "set_temp_unit:F"
            # or "set_temp_unit:C". Updates in-memory unit on the handler
            # (so it stamps every subsequent state) and persists to
            # config.yaml so the choice survives restarts.
            from spe.config import persist_temperature_unit
            requested = message.split(":", 1)[1].strip()
            applied = self._serial_handler.set_temperature_unit(requested)
            persist_temperature_unit(applied)
        elif self._serial_handler:
            self._serial_handler.send_command(message)

    # ------------------------------------------------------------------
    # Radio configuration (client-selected radio)
    # ------------------------------------------------------------------

    # Per-kind field rules for set_radio_config. The bundled dashboard
    # sends `Number(field) || <default>`, so a non-numeric entry arrives
    # as the default — but a *typed* out-of-range value (slice_rx 99, a
    # negative port) would otherwise be written straight to config.yaml
    # and only surface much later as a cryptic FlexProtocolError at tune
    # time. Range-check here so bad values never reach the file.
    # Each entry is (coercion, inclusive bounds).
    _RADIO_FIELD_RULES = {
        "flex": {
            "host": ("host", None),
            "port": ("int", (1, 65535)),
            "slice_rx": ("int", (0, 7)),        # SmartSDR allows 0-7 slices
            "tune_power_watts": ("int", (1, 100)),   # SPE wants 2-15W
        },
        "tci": {
            "host": ("host", None),
            "port": ("int", (1, 65535)),
            "trx": ("int", (0, 1)),             # ExpertSDR3 has TRX 0/1
            "mode": ("mode", None),
            "tune_drive": ("int", (0, 100)),    # percent; 0 = leave alone
        },
    }

    @classmethod
    def _coerce_radio_field(cls, section: str, field: str):
        """Return a validator for ``section.field``. Raises ValueError
        (message is client-facing) on anything the backend can't use."""
        rule, bounds = cls._RADIO_FIELD_RULES[section][field]

        def check(value):
            where = f"{section}.{field}"
            if rule == "host":
                host = str(value).strip()
                if len(host.split()) > 1 or len(host) > 255:
                    raise ValueError(f"{where}: {value!r} is not a hostname")
                return host
            if rule == "mode":
                mode = str(value).strip().upper()
                if not mode.isalnum() or len(mode) > 10:
                    raise ValueError(f"{where}: {value!r} is not a mode name")
                return mode
            # bool is an int subclass — reject it rather than silently
            # persisting True as 1.
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError(f"{where}: {value!r} is not a number")
            try:
                n = int(str(value).strip())
            except ValueError:
                raise ValueError(f"{where}: {value!r} is not a whole number")
            lo, hi = bounds
            if not lo <= n <= hi:
                raise ValueError(f"{where}: {n} is outside {lo}-{hi}")
            return n

        return check

    @classmethod
    def _radio_config_payload(cls, persisted: bool = True) -> str:
        """Serialise the current radio config for a client picker/form.

        ``persisted`` is False when the live change could not be written
        back to config.yaml — the client should show it as applying to
        this session only."""
        cfg = cls._app_config
        flex = cfg.flex if cfg else None
        tci = cfg.tci if cfg else None
        kind = cfg.radio.kind if cfg else "none"
        return json.dumps({
            "config_event": "radio",
            "persisted": persisted,
            "radio": {
                "kind": kind,
                "flex": {
                    "host": flex.host, "port": flex.port,
                    "slice_rx": flex.slice_rx,
                    "tune_power_watts": flex.tune_power_watts,
                } if flex else {},
                "tci": {
                    "host": tci.host, "port": tci.port, "trx": tci.trx,
                    "mode": tci.mode, "tune_drive": tci.tune_drive,
                } if tci else {},
            },
        })

    def _send_radio_config(self) -> None:
        """Send the current radio config to this client only."""
        try:
            self.write_message(self._radio_config_payload())
        except tornado.websocket.WebSocketClosedError:
            pass

    async def _handle_set_radio_config(self, payload: str) -> None:
        """Apply a client's radio-config change live, persist it, and
        broadcast the new config to every client. Payload is JSON:
        ``{"kind": "...", "flex": {...}, "tci": {...}}`` (sections
        optional). Refused while a tune cycle is running."""
        from dataclasses import replace
        from spe.config import persist_values

        cfg = self._app_config
        if cfg is None:
            return
        if self._tune_orchestrator and self._tune_orchestrator.is_running:
            AmplifierWebSocket.broadcast_tune_event(
                "RADIO_ERROR", "cannot change radio while a tune is running")
            return
        try:
            data = json.loads(payload)
        except (ValueError, TypeError) as e:
            AmplifierWebSocket.broadcast_tune_event(
                "RADIO_ERROR", f"bad set_radio_config payload: {e}")
            return

        kind = str(data.get("kind", cfg.radio.kind)).strip().lower()
        if kind not in ("flex", "tci", "none"):
            AmplifierWebSocket.broadcast_tune_event(
                "RADIO_ERROR", f"unknown radio kind {kind!r}")
            return

        # Validate every field the client sent *before* touching any live
        # state, so a bad value is refused whole instead of leaving a
        # half-applied config behind.
        sent: dict = {"flex": {}, "tci": {}}
        try:
            for section in ("flex", "tci"):
                block = data.get(section, {})
                if not isinstance(block, dict):
                    raise ValueError(f"{section}: expected an object")
                for field in self._RADIO_FIELD_RULES[section]:
                    if field in block:
                        sent[section][field] = self._coerce_radio_field(
                            section, field)(block[field])
        except ValueError as e:
            AmplifierWebSocket.broadcast_tune_event(
                "RADIO_ERROR", f"bad set_radio_config: {e}")
            return

        # Build the new config off to the side and hand it over in one
        # step: reconfigure() drops the open connection and swaps the
        # backend under the controller's own lock, so a radio_connect
        # from another client can't land on half-swapped state.
        new_radio = replace(cfg.radio, kind=kind)
        # Keep flex.enabled consistent with the selector for back-compat.
        new_flex = replace(cfg.flex, enabled=(kind == "flex"), **sent["flex"])
        new_tci = replace(cfg.tci, **sent["tci"])
        await self._radio_controller.reconfigure(new_radio, new_flex, new_tci)
        cfg.radio, cfg.flex, cfg.tci = new_radio, new_flex, new_tci

        changes = {"radio.kind": kind, "flex.enabled": new_flex.enabled}
        changes.update({f"flex.{k}": v for k, v in sent["flex"].items()})
        changes.update({f"tci.{k}": v for k, v in sent["tci"].items()})
        # persist_values only logs a warning on an I/O failure (read-only
        # mount, missing config.yaml). Don't let the client believe a
        # change stuck that the next restart will silently revert.
        persisted = persist_values(changes, self._config_path)
        logger.info("Radio config changed live: kind=%s (persisted=%s)",
                    kind, persisted)

        AmplifierWebSocket.broadcast_raw(
            self._radio_config_payload(persisted=persisted))
        AmplifierWebSocket.broadcast_tune_event(
            "RADIO_CONFIG_UPDATED",
            f"radio set to {kind}" if persisted else
            f"radio set to {kind} — this session only, not saved")
        if not persisted:
            AmplifierWebSocket.broadcast_tune_event(
                "RADIO_ERROR",
                f"could not write {self._config_path}: the radio change "
                "is live now but reverts on restart")

    async def _handle_power(self, command: str) -> None:
        """Handle power on/off commands asynchronously."""
        if command == "power_on":
            success = await self._power_controller.power_on()
        else:
            success = await self._power_controller.power_off()

        status = "ok" if success else "error"
        result = f'{{"power_result": "{command}", "status": "{status}"}}'

        # Notify all clients of the power action result
        dead_clients = set()
        for client in self.clients:
            try:
                client.write_message(result)
            except tornado.websocket.WebSocketClosedError:
                dead_clients.add(client)
        self.clients -= dead_clients

    def on_close(self) -> None:
        AmplifierWebSocket.clients.discard(self)
        logger.info(
            f"Client disconnected ({self.request.remote_ip}), "
            f"{len(self.clients)} remaining"
        )

    @classmethod
    def broadcast_state(cls, state: AmplifierState) -> None:
        """Broadcast amplifier state to all connected clients."""
        state_json = state.to_json()
        now = time.time()

        # Only broadcast if state changed or heartbeat interval elapsed
        if (
            state_json == cls._last_json
            and now - cls._last_broadcast_time < cls._heartbeat_interval
        ):
            return

        cls._last_json = state_json
        cls._last_broadcast_time = now

        dead_clients = set()
        for client in cls.clients:
            try:
                client.write_message(state_json)
            except tornado.websocket.WebSocketClosedError:
                dead_clients.add(client)

        cls.clients -= dead_clients

    @classmethod
    def broadcast_tune_event(cls, phase: str, message: str = "") -> None:
        """Relay a tune-orchestrator phase transition to all clients.

        Emits a small JSON object {"tune_event": phase, "tune_message":
        message, "ts": ts}. Clients latch the terminal phases SUCCESS /
        FAIL / ABORT to know the cycle is done; intermediate phases
        (LED_ON, CARRIER_ON, ...) drive progress UI."""
        msg = json.dumps({
            "tune_event": phase,
            "tune_message": message,
            "ts": time.time(),
        })
        cls.broadcast_raw(msg)

    @classmethod
    def broadcast_raw(cls, msg: str) -> None:
        """Broadcast an already-serialised JSON string to every connected
        client. Bypasses the state-dedup / min-interval gate that
        :meth:`broadcast_state` enforces. Use for presence heartbeats and
        any other message type whose cadence is driven independently of
        amp state changes."""
        dead_clients = set()
        for client in cls.clients:
            try:
                client.write_message(msg)
            except tornado.websocket.WebSocketClosedError:
                dead_clients.add(client)

        cls.clients -= dead_clients

    @classmethod
    def broadcast_rcu_frame(cls, payload: bytes) -> None:
        """Broadcast a raw RCU LCD display frame to all clients as a binary
        WebSocket message. The payload is the bytes after the ``AA AA AA 6A``
        sync+marker — i.e. what MacExpert's RCU frame parser expects. Clients
        that don't decode RCU (e.g. the bundled web dashboard) silently drop
        binary messages, so this is safe to broadcast to everyone."""
        if not cls.clients:
            return
        dead_clients = set()
        for client in cls.clients:
            try:
                client.write_message(payload, binary=True)
            except tornado.websocket.WebSocketClosedError:
                dead_clients.add(client)
        cls.clients -= dead_clients
