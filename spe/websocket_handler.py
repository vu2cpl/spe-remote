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
    _flex_controller = None
    _last_json = ""
    _last_broadcast_time = 0.0
    _heartbeat_interval = 15.0

    @classmethod
    def configure(cls, serial_handler, power_controller=None,
                  tune_orchestrator=None, flex_controller=None,
                  heartbeat: float = 15.0) -> None:
        cls._serial_handler = serial_handler
        cls._power_controller = power_controller
        cls._tune_orchestrator = tune_orchestrator
        cls._flex_controller = flex_controller
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
            # Sweep the SPE manual's recommended sub-bands for one
            # band. Operator picks band + antenna ahead of time; we
            # just tune at each freq the manual lists. Payload format:
            # tune_band:20m   (case-insensitive band name).
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
        elif message == "flex_connect":
            # Sent when a client opens its Sweep menu — pre-warm the
            # Flex connection so it's ready when the operator hits Start.
            # Idempotent; FlexController broadcasts FLEX_CONNECTING /
            # FLEX_CONNECTED / FLEX_ERROR so the UI can reflect status.
            # No-op when Flex is disabled — handled here (not forwarded
            # to the serial handler as an amp command).
            if self._flex_controller:
                import tornado.ioloop
                tornado.ioloop.IOLoop.current().spawn_callback(
                    self._flex_controller.connect
                )
        elif message == "flex_disconnect":
            # Sent when a client closes its Sweep menu while idle. Don't
            # drop the radio mid-tune — the orchestrator owns the
            # connection for the duration of a cycle and disconnects
            # itself when the cycle is over.
            if self._flex_controller and not (
                self._tune_orchestrator and self._tune_orchestrator.is_running
            ):
                import tornado.ioloop
                tornado.ioloop.IOLoop.current().spawn_callback(
                    self._flex_controller.disconnect
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
