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
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=_READY_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("TCI: no `ready;` within %.1fs — continuing", _READY_TIMEOUT)

        # Nudge the radio to (re)emit both TRX VFOs so the cache is fresh.
        await self._send(f"vfo:0,0;")
        await self._send(f"vfo:1,0;")
        logger.info("TCI: connected to %s (version=%r)", self.host, self.radio_version)

    async def close(self) -> None:
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):
                pass
            self._read_task = None
        if self._ws is not None:
            try:
                self._ws.close()
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
        assert self._ws is not None
        try:
            while True:
                msg = await self._ws.read_message()
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
        state = self.vfo_state.get(channel)
        if not state:
            return None
        freq = state.get("freq")
        mode = state.get("mode")
        if freq is None and mode is None:
            return None
        return {"channel": channel, "freq": freq, "mode": mode}

    async def restore(self, snap: Optional[dict]) -> None:
        if snap is None:
            return
        channel = snap["channel"]
        freq = snap.get("freq")     # Hz string straight from the radio
        mode = snap.get("mode")
        try:
            if freq is not None:
                await self._send(f"vfo:{channel},0,{int(freq)};")
            if mode is not None:
                await self._send(f"modulation:{channel},{mode};")
        except Exception:
            logger.exception("TCI: failed to restore vfo freq+mode")
