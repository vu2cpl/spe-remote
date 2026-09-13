"""Expert Electronics TCI backend for the SPE tune orchestrator.

TCI (Transceiver Control Interface) is the WebSocket text protocol spoken
by ExpertSDR3 / SunSDR-series radios. It is line/`;`-oriented, lowercase,
e.g. ``vfo:0,0,14025000;``. Default port is 50001.

This drives the same SM5TOG-style ATU tune flow the Flex backend does —
the commands are different but the shape is identical (set freq, set mode,
key the tune carrier). Command set verified against the reference
implementation https://github.com/sm5tog/sm5k-spe-tuner:

  * set frequency:  ``vfo:<trx>,0,<Hz>;``
  * set mode:       ``modulation:<trx>,CW;``
  * tune carrier:   ``tune:<trx>,true;`` / ``tune:<trx>,false;``
  * TX status in:   ``trx:<trx>,true|false`` (reliable; unlike tx_enable)

TCI works in **Hz**; :class:`spe.radio.RadioConnection` works in **MHz**.
The conversion lives at this boundary only — :meth:`TciConnection.set_frequency`
going out, :meth:`TciConnection.snapshot` coming back.

Transport is tornado's async WebSocket client, so no extra dependency: the
project already depends on tornado for the server side.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from tornado.websocket import websocket_connect, WebSocketClientConnection

from spe.radio import RadioConnection

logger = logging.getLogger(__name__)

TCI_PORT = 50001

# How long to wait for ExpertSDR3's initial state burst (it streams the
# current vfo/mode/etc. and ends with `ready;` right after connect). We
# wait for `ready` so snapshot() has freq+mode to restore, but don't fail
# if a firmware revision skips it.
_READY_TIMEOUT = 3.0
_CONNECT_TIMEOUT = 5.0


class TciConnection(RadioConnection):
    """Async TCI client for one ExpertSDR3 / SunSDR radio."""

    def __init__(self, host: str, port: int = TCI_PORT,
                 mode: str = "CW", tune_drive: int = 0):
        self.host = host
        self.port = port
        self.default_mode = mode or "CW"
        # Optional tune-drive percent (0-100). 0 ⇒ leave tune power to
        # ExpertSDR's own setting (don't send a drive command).
        self.tune_drive = int(tune_drive or 0)

        self._ws: Optional[WebSocketClientConnection] = None
        self._read_task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        # The TRX the orchestrator is driving — set by set_frequency /
        # set_mode and keyed by tune_carrier (which takes no channel arg,
        # to match the Flex interface).
        self._tx_channel = 0

        # Per-TRX cache populated from the radio's event stream: freq (Hz
        # as a string) and mode. Used by snapshot()/restore().
        self.vfo_state: dict[int, dict[str, str]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._ws is not None

    async def connect(self) -> None:
        url = f"ws://{self.host}:{self.port}/"
        logger.info("TCI: connecting to %s", url)
        self._ready.clear()
        self._ws = await websocket_connect(url, connect_timeout=_CONNECT_TIMEOUT)
        self._read_task = asyncio.ensure_future(self._read_loop())

        # ExpertSDR streams current state on connect and ends with
        # `ready;`. Wait for it (best effort) so snapshot() has data.
        #
        # Deliberately no VFO-query "nudge" here. TCI's request form for
        # a parameter is the command minus its value (`vfo:0,0;`), but
        # that grammar is ambiguous enough that a firmware revision could
        # read the missing third argument as *set VFO to 0 Hz* and trash
        # the operator's dial on every Sweep-menu open. The startup dump
        # already fills the cache; if some revision ever skips it, the
        # band check degrades to "radio band unknown" — which costs an
        # explicit band argument, not the operator's VFO.
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=_READY_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("TCI: no `ready;` within %.1fs — continuing", _READY_TIMEOUT)

        logger.info("TCI: connected to %s (version=%r)", self.host, self.radio_version)

    async def close(self) -> None:
        # Grab the socket first: cancelling the read task runs its
        # finally:, which clears self._ws — reading it afterwards would
        # find None and leak the real socket.
        ws = self._ws
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):
                pass
            self._read_task = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        self._ws = None
        self._ready.clear()

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------

    async def _send(self, message: str) -> None:
        if self._ws is None:
            raise ConnectionError("TCI not connected")
        # tornado's write_message returns a Future; await it so back-
        # pressure / write errors surface here rather than being swallowed.
        await self._ws.write_message(message)

    async def _read_loop(self) -> None:
        ws = self._ws
        assert ws is not None
        try:
            while True:
                msg = await ws.read_message()
                if msg is None:        # socket closed by radio
                    logger.info("TCI: socket closed by radio")
                    break
                if isinstance(msg, bytes):
                    continue           # TCI control channel is text-only
                # A single WS frame may carry several `;`-terminated cmds.
                for part in msg.split(";"):
                    self._dispatch(part.strip())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("TCI: read loop crashed")
        finally:
            # However we got here this socket is done, so drop it:
            # otherwise is_connected() keeps saying True after the radio
            # is power-cycled, the next tune skips the reconnect and
            # writes into a dead socket, and the operator has to
            # radio_disconnect + radio_connect by hand to recover.
            # Guarded so a reconnect's newer socket isn't clobbered by a
            # late-finishing old loop. Mirrors spe/flex.py's finally: —
            # when TCI grows a query-with-response, its pending futures
            # get failed here too rather than leaking awaiters.
            if self._ws is ws:
                self._ws = None
                self._ready.clear()

    def _dispatch(self, line: str) -> None:
        if not line:
            return
        head, _, rest = line.partition(":")
        head = head.lower()
        if head == "ready":
            self._ready.set()
            return
        if head == "vfo":
            # vfo:<trx>,<channel>,<freq_hz>
            parts = rest.split(",")
            if len(parts) >= 3:
                try:
                    trx = int(parts[0])
                    channel = int(parts[1])
                except ValueError:
                    return
                if channel == 0:       # VFO A — the one we tune
                    self.vfo_state.setdefault(trx, {})["freq"] = parts[2].strip()
            return
        if head == "modulation":
            # modulation:<trx>,<mode>
            parts = rest.split(",")
            if len(parts) >= 2:
                try:
                    trx = int(parts[0])
                except ValueError:
                    return
                self.vfo_state.setdefault(trx, {})["mode"] = parts[1].strip()
            return
        if head in ("device", "protocol") and not self.radio_version:
            # device:SunSDR2_PRO,...  /  protocol:ExpertSDR3,1.9
            self.radio_version = rest.strip()
            return

    # ------------------------------------------------------------------
    # RadioConnection interface
    # ------------------------------------------------------------------

    async def set_frequency(self, channel: int, freq_mhz: float) -> None:
        self._tx_channel = channel
        hz = int(round(freq_mhz * 1_000_000))
        await self._send(f"vfo:{channel},0,{hz};")

    async def set_mode(self, channel: int, mode: str) -> None:
        # TCI takes the mode verbatim ("CW" for the tune carrier).
        self._tx_channel = channel
        await self._send(f"modulation:{channel},{mode.strip().upper()};")

    async def set_tune_power(self, watts: int) -> None:
        # TCI has no per-watt tune-power command; ExpertSDR owns the tune
        # drive. Apply the configured percent only if the operator set one
        # (>0); otherwise leave the radio's own setting alone. The ``watts``
        # hint from the orchestrator is intentionally ignored here.
        if self.tune_drive > 0:
            await self._send(f"tune_drive:{self.tune_drive};")

    async def tune_carrier(self, on: bool) -> None:
        # Keyed per-TRX; channel == trx for TCI. The orchestrator sets the
        # frequency/mode on _tx_channel first, so key that same TRX.
        await self._send(f"tune:{self._tx_channel},{'true' if on else 'false'};")

    def snapshot(self, channel: int) -> Optional[dict]:
        """Freq crosses the RadioConnection interface in **MHz** (see
        spe/radio.py) — the TCI event cache holds the radio's own Hz, so
        convert here. The orchestrator's band check feeds this value
        straight to band_for_freq(), which is MHz-only: handing it Hz
        made every `tune_band('auto')` fail with "radio band unknown"
        and made an explicit band silently bypass the radio-rules
        safeguard."""
        state = self.vfo_state.get(channel)
        if not state:
            return None
        freq_hz = state.get("freq")
        mode = state.get("mode")
        freq_mhz = None
        if freq_hz is not None:
            try:
                freq_mhz = float(freq_hz) / 1_000_000.0
            except (TypeError, ValueError):
                logger.warning("TCI: unparsable vfo freq %r — "
                               "treating it as unknown", freq_hz)
        if freq_mhz is None and mode is None:
            return None
        return {"channel": channel, "freq": freq_mhz, "mode": mode}

    async def restore(self, snap: Optional[dict]) -> None:
        if snap is None:
            return
        channel = snap["channel"]
        freq_mhz = snap.get("freq")   # MHz — the interface unit
        mode = snap.get("mode")
        try:
            if freq_mhz is not None:
                # Reuse set_frequency's MHz→Hz conversion (and its
                # rounding) so snapshot and restore can't drift apart,
                # and so a fractional Hz can't raise the way int() did.
                await self.set_frequency(channel, float(freq_mhz))
            if mode is not None:
                # Verbatim: whatever sub-mode string the radio reported
                # is what it gets back.
                await self._send(f"modulation:{channel},{mode};")
        except Exception:
            # Log *and* re-raise. Swallowing left the orchestrator
            # emitting VFO_RESTORED while the radio was still parked on
            # the last swept sub-band; _restore() turns this into FAIL.
            logger.exception("TCI: failed to restore vfo freq+mode")
            raise
