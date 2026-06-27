"""FlexRadio 6000-series control via SmartSDR TCP/IP API.

Phase 1 of the spe-remote band-sweep work: a minimal async TCP client
that can drive a Flex 6000 hard enough to run the SM5TOG-style ATU
tune flow (set freq, set mode, key the built-in tune carrier).
``discover()`` (added 2026-06-19) listens for the radio's UDP
broadcast on port 4992 and pulls the IP from it, so the operator can
leave ``flex.host`` empty in config.yaml and have spe-remote find
the rig on its own. Static config still wins when set.

Protocol summary (from
https://github.com/flexradio/smartsdr-api-docs/wiki/SmartSDR-TCPIP-API):

  * TCP port 4992, line-oriented text protocol.
  * Outbound command:  C<seq>|<command>\\n
  * Inbound reply:     R<seq>|<hex_status>|<message>   (status 0 == ok)
  * Inbound async:     S<handle>|<message>            (subscribed events)
  * On connect the radio sends a banner — V<version> and H<handle>
    lines — before any reply traffic.

This module deliberately does NOT touch the existing serial-side
machinery in spe-remote. spe.server can stay oblivious to Flex until
Phase 2 wires the two together.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

FLEX_TCP_PORT = 4992

# SmartSDR uses UDP port 4992 for radio discovery. Flex 6000-series
# radios broadcast a text packet roughly once per second from their
# LAN IP; `discover()` listens for one and pulls the IP from the
# `ip=<a.b.c.d>` field. Used as a fallback when `flex.host` is empty
# in the config.
FLEX_DISCOVERY_PORT = 4992
_DISCOVERY_TIMEOUT = 5.0  # seconds — radio broadcasts ~1Hz so 5s is plenty

# How long to wait for a command reply before giving up and surfacing
# an error. SmartSDR replies are typically tens of milliseconds, so 5 s
# is generous. Tune state changes are NOT bounded by this — they arrive
# as async S<handle> messages on the subscription channel.
_REPLY_TIMEOUT = 5.0

# How long the radio has to send the V/H banner after the TCP socket
# opens. Real radios reply within a few hundred ms; the timeout exists
# to fail loudly if you point spe-remote at something that ISN'T a Flex.
_BANNER_TIMEOUT = 3.0


class FlexProtocolError(Exception):
    """Raised when the radio rejects a command (non-zero status code)."""


class FlexConnection:
    """Async client for one Flex 6000-series radio.

    Lifecycle:

        flex = FlexConnection("192.168.1.148")
        await flex.connect()                  # banner read, reader task started
        await flex.set_slice_mode(0, "CWU")
        await flex.set_slice_freq(0, 14.020)
        await flex.set_tune_power(10)
        await flex.tune_carrier(on=True)      # ATU on the amp can now sweep
        ...
        await flex.tune_carrier(on=False)
        await flex.close()

    The connection auto-reconnects nothing — Phase 2 will add a
    supervising loop. For now, a dropped socket raises on the next
    send.
    """

    def __init__(self, host: str, port: int = FLEX_TCP_PORT):
        self.host = host
        self.port = port

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._reader_task: Optional[asyncio.Task] = None

        # Monotonic sequence number for command framing. SmartSDR allows
        # up to 32-bit; we never recycle (a session lives minutes).
        self._seq = 0
        # Pending command futures keyed by sequence number.
        self._pending: dict[int, asyncio.Future] = {}

        # Banner data the radio volunteers right after connect.
        self.radio_version: str = ""
        self.client_handle: str = ""

        # Subscription callback: invoked for every S<handle>|<msg>.
        # Set after connect() if you want streamed updates.
        self.on_status: Optional[Callable[[str, str], None]] = None

        # Live cache of per-slice state, populated from subscribed
        # `slice N k=v k=v …` events. Keys are slice index ints;
        # values are dicts of the latest fields the radio has emitted
        # (most importantly RF_frequency in MHz as a string, and mode
        # as a string like "CWU" / "USB"). Useful for save / restore
        # around tune cycles. Empty until the first sub event arrives.
        self.slice_state: dict[int, dict[str, str]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the TCP socket, read the banner, start the reader loop."""
        logger.info(f"Flex: connecting to {self.host}:{self.port}")
        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port
        )

        # Banner: a few lines starting with V (version) and H (handle).
        # The radio sometimes batches them, sometimes doesn't — read
        # until either both are seen or the timeout fires.
        try:
            await asyncio.wait_for(self._read_banner(), timeout=_BANNER_TIMEOUT)
        except asyncio.TimeoutError:
            # Don't fail here — some firmware revisions skip one of the
            # banner lines. We can still issue commands; just leave the
            # field empty and warn.
            logger.warning(
                "Flex: banner incomplete after %.1fs "
                "(version=%r handle=%r). Continuing.",
                _BANNER_TIMEOUT, self.radio_version, self.client_handle,
            )

        self._reader_task = asyncio.create_task(self._read_loop())
        logger.info(
            f"Flex: connected (version={self.radio_version!r}, "
            f"handle={self.client_handle!r})"
        )

        # Subscribe to slice events so slice_state populates
        # automatically. Tolerate failure here — slice_state stays
        # empty in that case, and any consumer (e.g. the orchestrator's
        # save/restore) will fall back gracefully.
        try:
            await self.send("sub slice all")
        except (FlexProtocolError, asyncio.TimeoutError, ConnectionError):
            logger.warning("Flex: could not subscribe to slice events; "
                           "slice_state will stay empty")

    @property
    def is_connected(self) -> bool:
        """True while the TCP socket is open and the reader loop is live.

        Used by :class:`spe.flex_controller.FlexController` to make
        connect/disconnect idempotent in the on-demand lifecycle."""
        return self._writer is not None and self._reader_task is not None

    async def close(self) -> None:
        """Shut down the connection. Idempotent."""
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._writer = None
        self._reader = None
        # Cancel any in-flight commands so callers don't hang forever.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("Flex connection closed"))
        self._pending.clear()

    # ------------------------------------------------------------------
    # Low-level send/receive
    # ------------------------------------------------------------------

    async def send(self, command: str) -> str:
        """Send a command, return the reply's message field.

        Raises ``FlexProtocolError`` if the radio reports a non-zero
        status. Raises ``ConnectionError`` if the socket is closed.
        Raises ``asyncio.TimeoutError`` if no reply arrives within
        ``_REPLY_TIMEOUT``.
        """
        if self._writer is None:
            raise ConnectionError("Flex not connected")
        self._seq += 1
        seq = self._seq
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[seq] = fut

        line = f"C{seq}|{command}\n"
        self._writer.write(line.encode("ascii", errors="replace"))
        try:
            await self._writer.drain()
        except Exception as e:
            self._pending.pop(seq, None)
            raise ConnectionError(f"Flex write failed: {e}") from e

        try:
            status, message = await asyncio.wait_for(fut, timeout=_REPLY_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(seq, None)
            raise
        if status != 0:
            raise FlexProtocolError(
                f"Flex rejected {command!r}: status=0x{status:08X} msg={message!r}"
            )
        return message

    async def _read_banner(self) -> None:
        """Read the radio's V/H banner. Stops when both seen, or on
        first line that doesn't look like banner content."""
        assert self._reader is not None
        seen_v = seen_h = False
        while not (seen_v and seen_h):
            line = await self._reader.readline()
            if not line:
                return
            stripped = line.decode("ascii", errors="replace").rstrip()
            if stripped.startswith("V"):
                self.radio_version = stripped[1:]
                seen_v = True
            elif stripped.startswith("H"):
                self.client_handle = stripped[1:]
                seen_h = True
            else:
                # Unexpected — push back via a small queue isn't worth
                # it; just dispatch it as if from the read loop. In
                # practice the banner is V then H, nothing else.
                self._dispatch_line(stripped)
                return

    async def _read_loop(self) -> None:
        """Consume lines from the radio, dispatch R replies and S events."""
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    logger.info("Flex: socket closed by radio")
                    break
                self._dispatch_line(line.decode("ascii", errors="replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Flex: read loop crashed")
        finally:
            # Wake up any callers stuck on send().
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("Flex read loop ended"))
            self._pending.clear()

    def _dispatch_line(self, line: str) -> None:
        if not line:
            return
        # R<seq>|<hex_status>|<message>
        if line.startswith("R"):
            parts = line[1:].split("|", 2)
            try:
                seq = int(parts[0])
                status = int(parts[1], 16)
            except (ValueError, IndexError):
                logger.warning(f"Flex: malformed reply line: {line!r}")
                return
            message = parts[2] if len(parts) > 2 else ""
            fut = self._pending.pop(seq, None)
            if fut is not None and not fut.done():
                fut.set_result((status, message))
            return
        # S<handle>|<message>
        if line.startswith("S"):
            parts = line[1:].split("|", 1)
            handle = parts[0]
            message = parts[1] if len(parts) > 1 else ""
            # Update slice_state cache first so any save/restore
            # consumer sees fresh values even if it's reading on the
            # same callback tick as the event arrives.
            self._update_slice_cache(message)
            if self.on_status is not None:
                try:
                    self.on_status(handle, message)
                except Exception:
                    logger.exception("Flex: on_status callback raised")
            return
        # V / H banner lines after the initial banner are unusual but
        # not destructive — log at debug.
        logger.debug(f"Flex: unhandled line: {line!r}")

    def _update_slice_cache(self, message: str) -> None:
        """If ``message`` is a ``slice <N> <key=value> …`` event, merge
        the keys into self.slice_state[N]. Silently ignores anything
        else (radio / interlock / eq events). Keeps the cache as the
        most recent value for each key — partial slice events (radio
        only emits the fields that changed) are merged, never replaced
        wholesale."""
        if not message.startswith("slice "):
            return
        rest = message[6:].split(maxsplit=1)
        if len(rest) < 2:
            return
        try:
            slice_n = int(rest[0])
        except ValueError:
            return
        state = self.slice_state.setdefault(slice_n, {})
        # Parse simple space-separated key=value tokens. Values do not
        # contain spaces in the wire format (the radio quotes anything
        # containing spaces with an underscore at the protocol layer).
        for token in rest[1].split():
            if "=" in token:
                k, v = token.split("=", 1)
                state[k] = v

    # ------------------------------------------------------------------
    # High-level convenience methods
    # ------------------------------------------------------------------
    #
    # All values match the SmartSDR API formats verbatim. Frequencies
    # in MHz (e.g. 14.020000), powers in 0-100 watts, modes as the
    # string the radio expects ("CWU", "CWL", "USB", "LSB", ...).

    async def set_slice_freq(self, slice_rx: int, freq_mhz: float) -> None:
        """Tune slice <slice_rx> to <freq_mhz>. Up to 15 significant
        digits; 6 dp covers Hz resolution to 5 MHz, fine for us."""
        await self.send(f"slice t {slice_rx} {freq_mhz:.6f}")

    async def set_slice_mode(self, slice_rx: int, mode: str) -> None:
        """Set slice <slice_rx> to mode (e.g. CWU, USB, LSB)."""
        await self.send(f"slice s {slice_rx} mode={mode}")

    async def set_tune_power(self, watts: int) -> None:
        """Set the tune-carrier output power in watts (0-100)."""
        if not 0 <= watts <= 100:
            raise ValueError(f"tune power must be 0-100 W, got {watts}")
        await self.send(f"transmit set tunepower={watts}")

    async def tune_carrier(self, on: bool) -> None:
        """Start (on=True) or stop (on=False) the built-in tune
        carrier. While on, the radio emits a CW carrier at the
        configured tune power on the active TX slice — exactly the
        clean steady drive the SPE's ATU wants."""
        await self.send(f"transmit tune {'on' if on else 'off'}")

    async def mox(self, on: bool) -> None:
        """Raw PTT (xmit 1/0). Not normally needed for the tune flow
        — `tune_carrier` is preferred because it handles mode +
        keying together — but provided for completeness."""
        await self.send(f"xmit {1 if on else 0}")

    async def subscribe(self, topic: str) -> None:
        """Subscribe to async updates. Common topics: ``slice 0``,
        ``transmit``, ``radio``. Updates arrive via ``on_status``."""
        await self.send(f"sub {topic} all")

    async def slice_list(self) -> str:
        """Read-only: return the slice list as the radio reports it.
        Useful for confirming connectivity without keying anything."""
        return await self.send("slice list")


# ──────────────────────────────────────────────────────────────────
# UDP discovery
# ──────────────────────────────────────────────────────────────────

async def discover(timeout: float = _DISCOVERY_TIMEOUT) -> Optional[dict]:
    """Listen for one SmartSDR discovery broadcast and return its
    parsed fields, or None on timeout.

    The Flex 6000-series radios broadcast a UDP packet to
    255.255.255.255:4992 roughly once per second containing
    space-separated `key=value` pairs. Typical fields include:
    ``discovery`` (literal marker), ``model``, ``serial``, ``version``,
    ``nickname``, ``callsign``, ``ip``, ``port``, ``status``.

    Returns a dict with at minimum ``ip`` and ``port`` populated, plus
    any other fields the radio chose to broadcast (used downstream
    for logging which radio we found — nickname, callsign, etc.).
    Returns None if no packet arrives within ``timeout``.

    Pi-side usage: server.py falls back to this when ``flex.host`` is
    empty. Multi-Flex networks deliberately not handled here — we
    take the first packet that arrives, which is fine when one radio
    is on the shack LAN; configurable matching can come later.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        # 0.0.0.0 catches both broadcast and (most) multicast frames
        # the radio emits without needing IGMP-join gymnastics on the
        # Pi side. Doesn't conflict with a TCP listener on the same
        # port (different socket type).
        sock.bind(("0.0.0.0", FLEX_DISCOVERY_PORT))
    except OSError as e:
        logger.error(f"Flex discovery bind failed on UDP {FLEX_DISCOVERY_PORT}: {e}")
        sock.close()
        return None

    loop = asyncio.get_event_loop()

    def _recv_once() -> Optional[bytes]:
        try:
            data, _addr = sock.recvfrom(2048)
            return data
        except OSError:
            return None

    try:
        sock.settimeout(timeout)
        data = await loop.run_in_executor(None, _recv_once)
    finally:
        sock.close()

    if not data:
        return None

    text = data.decode("ascii", errors="replace").strip()
    fields: dict[str, str] = {}
    for token in text.split():
        if "=" in token:
            k, v = token.split("=", 1)
            fields[k] = v
    if "ip" not in fields:
        logger.warning(f"Flex discovery packet had no ip= field: {text!r}")
        return None
    fields.setdefault("port", str(FLEX_TCP_PORT))
    return fields
