"""Async serial handler for SPE amplifier communication.

Uses plain pyserial (NOT serial_asyncio) on a background daemon thread for
reads. The thread pushes raw chunks into an asyncio queue via
``call_soon_threadsafe``; the asyncio event loop drains the queue and does
the framing. Writes go through a ``threading.Lock``-guarded wrapper so
coroutines never interleave packets on the wire.

Why not serial_asyncio: its internal ``_read_ready`` callback routinely
raises ``SerialException("readiness to read but returned no data")`` on
USB-serial adapters under moderate traffic, which bounces the port and
stalls RCU frame delivery. The blocking ``serial.Serial.read(...)`` call
used here never hits that code path, so the port stays up even under the
full RCU stream.

Parses two response types on the same byte stream:
  * CSV status frames (``AA AA AA`` + CNT=0x43 + 67 bytes + checksum + CRLF)
  * RCU LCD display frames (``AA AA AA`` + type=0x6A + variable payload,
    terminated by the next sync or a quiet period)

CSV frames feed ``on_state_update``; RCU frames feed ``on_rcu_frame``.
"""

from __future__ import annotations  # Allow PEP 604 unions (X | None) on Python 3.9

import asyncio
import logging
import threading
import time
from typing import Callable, Optional

import serial
import serial.tools.list_ports

from spe.config import SerialConfig, PollingConfig
from spe.protocol import (
    CMD_REQUEST, CMD_RCU_ON, CMD_RCU_OFF, COMMANDS,
    RESP_STATUS_CNT, RESP_RCU_TYPE,
    AmplifierState, parse_status,
)

logger = logging.getLogger(__name__)

# RCU OFF->ON cycle cadence. The amp only emits one frame per
# RCU_ON request AND only when the display has changed since the
# last frame, so this interval is the worst-case latency for the
# app to discover front-panel events (TUNE LED toggle, cursor
# nav, screen switch). 0.5 s gives near-real-time tracking of
# the TUNE LED while staying clear of the amp's serial-buffer
# saturation envelope. Previously 1.5 s — bumped down 2026-06-19
# when MacExpert's TUNE LED indicator (driven by byte 4 bit 6 in
# the RCU frame) felt noticeably laggy against the front panel.
# If a regression appears (cursor flicker, freezes, dropped
# frames), back off to 1.0 s before going further.
_RCU_TICK_INTERVAL = 0.5  # seconds
_RCU_OFF_ON_GAP = 0.05    # seconds

# Force-flush an unterminated RCU frame if no new bytes arrive for this
# long. Covers static screens where the amp sends one frame and then goes
# silent until the next tick.
_RCU_QUIET_FLUSH = 0.3    # seconds

# Cap the receive buffer to prevent unbounded growth on a stuck parser.
_MAX_BUFFER = 4096

# Blocking serial read timeout. Balances responsiveness against CPU churn
# in the reader thread. 100 ms is plenty — chunks arrive as fast as the
# amp sends them.
_READ_TIMEOUT = 0.1


class SerialHandler:
    """Manages serial communication with the SPE amplifier.

    Threading model:
      * Asyncio loop owns: command queue, frame parsing, callbacks.
      * Daemon thread owns: blocking reads from the serial port.
      * The thread posts byte chunks to asyncio via ``call_soon_threadsafe``.
      * Writes are synchronous from asyncio (pyserial's Serial.write is
        fast) but guarded by ``self._write_lock`` (threading.Lock) so the
        reader thread never sees a half-written packet.
    """

    def __init__(
        self,
        serial_config: SerialConfig,
        polling_config: PollingConfig,
        on_state_update: Callable[[AmplifierState], None],
        on_rcu_frame: Optional[Callable[[bytes], None]] = None,
        temperature_unit: str = "C",
    ):
        self.serial_config = serial_config
        self.polling_config = polling_config
        self.on_state_update = on_state_update
        self.on_rcu_frame = on_rcu_frame
        # Stamped onto every parsed state so clients can render the right
        # unit symbol. The amp itself doesn't tell us — has to be set
        # in config.yaml to match the front-panel setup menu.
        self.temperature_unit = "F" if str(temperature_unit).upper().startswith("F") else "C"

        self._port: serial.Serial | None = None
        self._connected = False
        self._running = False
        self._state = AmplifierState(temperature_unit=self.temperature_unit)

        self._command_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._write_lock = threading.Lock()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_reader = threading.Event()

        self._buffer = bytearray()
        self._last_byte_at: float = 0.0
        # Optional raw-byte capture for protocol investigation. When
        # serial.debug_raw_log is set in config.yaml, every chunk the
        # reader receives is appended as "<monotonic> <hex>\n" before
        # framing is attempted, so we can see frame types the parser
        # drops (anything that isn't CSV CNT=0x43 or RCU type=0x6A).
        # Off by default — production should never run with it enabled.
        self._raw_log_file = None
        raw_path = getattr(serial_config, "debug_raw_log", "") or ""
        if raw_path:
            try:
                # Buffered text mode; flush after each write so a SIGTERM
                # mid-capture still keeps everything to that point.
                self._raw_log_file = open(raw_path, "a", buffering=1)
                self._raw_log_file.write(
                    f"# spe-remote raw capture begin {time.monotonic():.3f}\n"
                )
                logger.warning(
                    f"DEBUG RAW LOG ON: every received byte → {raw_path}. "
                    "Disable in production (file grows unbounded)."
                )
            except OSError as e:
                logger.error(f"Could not open debug_raw_log={raw_path!r}: {e}")
                self._raw_log_file = None
        # Monotonic timestamp of the most recently parsed CSV state frame.
        # 0.0 means "no frame yet seen this session." Used to detect the
        # amp going dark even though the FTDI port is still open (e.g.
        # amp powered off while the USB-serial cable stays connected to
        # the Pi). Exposed via ``last_state_age``.
        self._last_state_at: float = 0.0
        # Same idea for RCU display frames. STANDBY emits CSV slower than
        # the alive-threshold, so without an RCU liveness signal the
        # presence heartbeat would flip to serial:"down" even though the
        # amp is fine. Exposed via ``last_rcu_age``.
        self._last_rcu_at: float = 0.0
        # Last broadcast op_status — used to log transitions at WARNING
        # so unexpected Oper↔Stby flips during TX (typical RF-on-FTDI
        # corruption signature) show up in journald without DEBUG noise.
        # Diagnostic-only; no behaviour change.
        self._prev_op_status: str = ""

        # --- p_out averaging (EMA) -----------------------------------
        # Smoothing factor: higher = faster response / less smoothing.
        # 0.15 settles roughly over ~1s at the 25 Hz TX poll rate
        # (tx_interval=0.04s in config.yaml), which is in the same
        # ballpark as a typical "AVG" meter ballistic on a transceiver.
        # Tune by feel if it reads too twitchy or too sluggish.
        self._pout_avg_alpha: float = 0.15
        self._pout_avg: float | None = None
        # Tracks the (op_status, tx_status) pair the average was last
        # computed for. On any change — most importantly RX -> TX at the
        # start of a transmission — we reset rather than smooth, so the
        # first few samples of a new transmission aren't dragged down by
        # whatever idle/zero reading preceded it.
        self._pout_avg_key: tuple[str, str] | None = None

        # --- p_out peak-hold ------------------------------------------
        # How long the held peak stays pinned before it starts decaying
        # back toward the live reading. 2.5s matches the de-facto PEP
        # hold convention on most modern transceivers.
        self._pout_peak_hold_s: float = 2.5
        # Once the hold window expires, how fast the displayed peak falls
        # back toward the current reading, in watts per second. Purely
        # cosmetic (stops the number "snapping" downward, which reads as
        # jumpy in exactly the way this feature exists to avoid). A full
        # 1500W -> 0W fall takes ~2.5s. The fall is scaled by real elapsed
        # time between frames, so the rate is independent of the sample
        # rate (see _consume_csv_frame).
        self._pout_peak_decay_w_per_s: float = 600.0
        self._pout_peak: float = 0.0
        # Monotonic timestamp the hold window is measured from (set when
        # _pout_peak was last pinned: a new high, or a reset transition).
        self._pout_peak_at: float = 0.0
        # Monotonic timestamp of the previous frame — the decay step
        # multiplies the W/s rate by (now - this) so the fall is a true
        # constant W/s regardless of how fast frames arrive.
        self._pout_peak_prev_tick: float = 0.0
        # Same reset-on-transition rule as the average — see
        # _pout_avg_key above for why.
        self._pout_peak_key: tuple[str, str] | None = None
        # TUNE LED state from the latest RCU frame's byte 4 bit 6.
        # False until at least one RCU frame has arrived; thereafter
        # mirrors the front-panel LED with at most _RCU_TICK_INTERVAL
        # latency. Used by the Phase-2 tune orchestrator and exposed
        # to WS clients as the ``tune_active`` field on each state
        # broadcast.
        self._last_tune_active: bool = False

    @property
    def state(self) -> AmplifierState:
        return self._state

    @property
    def last_state_age(self) -> float:
        """Seconds since the last successful CSV state-frame parse.

        Returns ``float('inf')`` if no frame has been seen this session.
        Use this to detect "amp is off but port is still open" — the
        ``connected`` property only reflects USB-FTDI link state, which
        stays True even when the amp's CPU is dead.
        """
        if self._last_state_at == 0.0:
            return float("inf")
        return time.monotonic() - self._last_state_at

    @property
    def last_rcu_age(self) -> float:
        """Seconds since the most recent RCU display frame arrived.

        Returns ``float('inf')`` if no RCU frame has been seen this
        session. Pair with ``last_state_age`` for a true "amp on the
        wire" signal — STANDBY slows CSV below the heartbeat threshold
        but the RCU OFF→ON ticker keeps frames flowing every ~1.5 s.
        """
        if self._last_rcu_at == 0.0:
            return float("inf")
        return time.monotonic() - self._last_rcu_at

    @property
    def last_tune_active(self) -> bool:
        """True iff the most recent RCU frame had byte 4 bit 6 CLEAR,
        meaning the front-panel TUNE LED is on. Stays False until at
        least one frame has arrived; thereafter mirrors the LED with
        at most ``_RCU_TICK_INTERVAL`` (currently 0.5 s) latency.

        Read by the Phase-2 tune orchestrator to gate the Flex carrier
        transitions (carrier comes on after we confirm True; we wait
        for False before stepping to the next sweep frequency).
        """
        return self._last_tune_active

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._loop = asyncio.get_running_loop()
        while self._running:
            try:
                await self._open_port()
                await self._run_loop()
            except (serial.SerialException, OSError) as e:
                logger.error(f"Serial error: {e}")
            finally:
                self._teardown_port()
            if self._running:
                logger.info("Reconnecting in 3 seconds...")
                await asyncio.sleep(3)

    async def stop(self) -> None:
        self._running = False
        self._stop_reader.set()
        if self._port and self._port.is_open:
            try:
                self._safe_write(CMD_RCU_OFF)
            except Exception:
                pass
        self._teardown_port()

    def send_command(self, command: str) -> None:
        cmd_bytes = COMMANDS.get(command)
        if cmd_bytes:
            self._command_queue.put_nowait(cmd_bytes)
            logger.info(f"Queued command: {command}")
        else:
            logger.warning(f"Unknown command: {command}")

    def set_temperature_unit(self, unit: str) -> str:
        """Update the unit stamped onto subsequent state broadcasts.

        Also patches the cached _state and re-fires the on_state_update
        callback, so connected clients flip immediately without having
        to wait for the next CSV poll. Returns the normalised unit ('C'
        or 'F') that was actually set.
        """
        unit = "F" if str(unit).upper().startswith("F") else "C"
        if unit != self.temperature_unit:
            self.temperature_unit = unit
            self._state.temperature_unit = unit
            try:
                self.on_state_update(self._state)
            except Exception as e:
                logger.warning(f"State callback during unit change failed: {e}")
            logger.info(f"Temperature unit changed to {unit}")
        return unit

    # ------------------------------------------------------------------
    # Port open / close
    # ------------------------------------------------------------------

    async def _open_port(self) -> None:
        logger.info(
            f"Connecting to {self.serial_config.port} "
            f"at {self.serial_config.baudrate} baud..."
        )
        port = serial.Serial(
            port=self.serial_config.port,
            baudrate=self.serial_config.baudrate,
            timeout=_READ_TIMEOUT,
            write_timeout=1.0,
        )
        self._port = port
        self._connected = True
        self._buffer.clear()
        self._last_byte_at = 0.0
        logger.info("Serial connected")

        # Fire up the reader thread.
        self._stop_reader.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="spe-serial-reader",
            daemon=True,
        )
        self._reader_thread.start()

        # Initial status request + enable RCU.
        self._safe_write(CMD_REQUEST)
        self._safe_write(CMD_RCU_ON)

    def _teardown_port(self) -> None:
        """Close the port and signal the reader thread to exit. Idempotent."""
        self._connected = False
        self._stop_reader.set()

        # Drop queued user commands so stale presses don't fire on a
        # different menu after the reconnect completes.
        try:
            while not self._command_queue.empty():
                self._command_queue.get_nowait()
        except Exception:
            pass

        port = self._port
        self._port = None
        if port is not None:
            try:
                if port.is_open:
                    port.close()
            except Exception:
                pass

        # Don't join the reader thread here — we're potentially on the
        # same thread or need to avoid blocking the event loop. The
        # thread exits naturally once its serial handle is closed.

    # ------------------------------------------------------------------
    # Write path (asyncio side, but synchronous)
    # ------------------------------------------------------------------

    def _safe_write(self, payload: bytes) -> None:
        port = self._port
        if port is None or not port.is_open:
            logger.warning(
                f"Serial write skipped: port {'None' if port is None else 'closed'} "
                f"(payload={payload.hex()})"
            )
            return
        # Back-pressure: if the OS-level write buffer is already piling
        # up (FTDI hasn't drained yet), drop low-priority RCU heartbeat
        # writes rather than queueing more. RCU_OFF/RCU_ON are 0x80/0x81
        # — losing one tick is harmless; the next tick will produce a
        # frame. Dropping a user command (any other code) would lose a
        # button press, so we always commit those.
        is_rcu_tick = payload in (CMD_RCU_OFF, CMD_RCU_ON, CMD_REQUEST)
        try:
            waiting = port.out_waiting
        except Exception:
            waiting = 0
        if is_rcu_tick and waiting > 64:
            logger.debug(
                f"Backpressure: skipping {payload.hex()} "
                f"(out_waiting={waiting})"
            )
            return
        with self._write_lock:
            try:
                logger.debug(f"Serial write: {payload.hex()}")
                port.write(payload)
                # flush() forces the kernel write buffer to drain. Without
                # it, FTDI writes accumulate in the kernel queue and the
                # driver eventually wedges (writes silently succeed at OS
                # level but never reach the device). With the heartbeat
                # throttled to 1.5 s and back-pressure above, flush()
                # completes in microseconds; only a hardware-stuck device
                # would block here, in which case we want the exception.
                port.flush()
            except (serial.SerialException, OSError) as e:
                logger.warning(f"Serial write failed: {e}")

    # ------------------------------------------------------------------
    # Reader thread
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        port = self._port
        loop = self._loop
        if port is None or loop is None:
            return
        spurious_count = 0
        while not self._stop_reader.is_set():
            try:
                data = port.read(256)
            except serial.SerialException as e:
                msg = str(e)
                if "readiness to read but returned no data" in msg:
                    # Linux USB-serial kernel-level spurious readable flag.
                    # The port is actually fine — just the poll() flag is
                    # lying. Retry rather than tear down the connection.
                    spurious_count += 1
                    if spurious_count % 100 == 1:
                        logger.debug(
                            f"Suppressed spurious USB-serial poll glitch "
                            f"(count={spurious_count})"
                        )
                    # Tiny sleep so we don't spin at 100% CPU if the glitch
                    # is rapid-fire.
                    time.sleep(0.005)
                    continue
                logger.error(f"Serial read failed: {e}")
                loop.call_soon_threadsafe(self._signal_disconnect)
                return
            except OSError as e:
                logger.error(f"Serial read OS error: {e}")
                loop.call_soon_threadsafe(self._signal_disconnect)
                return
            if not data:
                # Timeout with no data — not an error, just continue.
                continue
            spurious_count = 0  # Real data flushes the glitch state.
            loop.call_soon_threadsafe(self._ingest_chunk, bytes(data))

    def _signal_disconnect(self) -> None:
        """Called on the asyncio loop when the reader thread has bailed.
        Tearing down the port here causes ``_run_loop`` to exit which kicks
        off a reconnect from ``start``'s outer loop."""
        self._stop_reader.set()
        # Closing the port makes any pending write raise, and the run loop
        # tasks will finish naturally.
        port = self._port
        if port is not None:
            try:
                if port.is_open:
                    port.close()
            except Exception:
                pass

    def _ingest_chunk(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self._raw_log_file is not None:
            # One line per chunk, monotonic seconds (5 dp = 10 µs) so we
            # can align with parse-side events later. Hex with no spaces
            # for compactness — analysis tools can split on byte width.
            try:
                self._raw_log_file.write(
                    f"{time.monotonic():.5f} {chunk.hex()}\n"
                )
            except OSError as e:
                logger.error(f"raw log write failed (disabling): {e}")
                try:
                    self._raw_log_file.close()
                except Exception:
                    pass
                self._raw_log_file = None
        self._buffer.extend(chunk)
        self._last_byte_at = time.monotonic()
        if len(self._buffer) > _MAX_BUFFER:
            logger.warning("Receive buffer overflowed, dropping stale bytes")
            self._buffer.clear()
            return
        self._drain_buffer()

    # ------------------------------------------------------------------
    # Asyncio background loops
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        poll_task = asyncio.create_task(self._poll_loop())
        cmd_task = asyncio.create_task(self._command_loop())
        rcu_task = asyncio.create_task(self._rcu_tick_loop())
        flush_task = asyncio.create_task(self._quiet_flush_loop())
        watchdog_task = asyncio.create_task(self._connection_watchdog())

        tasks = [poll_task, cmd_task, rcu_task, flush_task, watchdog_task]
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    raise exc
        finally:
            for task in tasks:
                task.cancel()
            # Await cancellations so they don't log "was never retrieved".
            for task in tasks:
                try:
                    await task
                except BaseException:
                    pass

    async def _connection_watchdog(self) -> None:
        """Returns (exits the _run_loop) once the port has been torn down
        by a reader-thread error. Keeps the outer reconnect logic simple."""
        while self._running:
            if self._port is None or not self._port.is_open:
                return
            await asyncio.sleep(0.25)

    async def _poll_loop(self) -> None:
        while self._running:
            interval = (
                self.polling_config.tx_interval
                if self._state.is_active
                else self.polling_config.idle_interval
            )
            await asyncio.sleep(interval)
            if self._command_queue.empty():
                self._safe_write(CMD_REQUEST)

    async def _command_loop(self) -> None:
        while self._running:
            cmd = await self._command_queue.get()
            self._safe_write(cmd)

    async def _rcu_tick_loop(self) -> None:
        while self._running:
            await asyncio.sleep(_RCU_TICK_INTERVAL)
            self._safe_write(CMD_RCU_OFF)
            await asyncio.sleep(_RCU_OFF_ON_GAP)
            self._safe_write(CMD_RCU_ON)

    async def _quiet_flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(_RCU_QUIET_FLUSH / 2)
            if not self._buffer:
                continue
            if self._last_byte_at == 0.0:
                continue
            if time.monotonic() - self._last_byte_at < _RCU_QUIET_FLUSH:
                continue
            self._flush_open_rcu_frame()

    # ------------------------------------------------------------------
    # Frame extraction (same as the serial_asyncio version)
    # ------------------------------------------------------------------

    def _drain_buffer(self) -> None:
        while True:
            sync = self._find_sync(0)
            if sync is None:
                if len(self._buffer) > 3:
                    del self._buffer[:-3]
                return
            if sync > 0:
                del self._buffer[:sync]
            if len(self._buffer) < 4:
                return

            marker = self._buffer[3]
            if marker == RESP_STATUS_CNT:
                if not self._consume_csv_frame():
                    return
            elif marker == RESP_RCU_TYPE:
                if not self._consume_rcu_frame():
                    return
            else:
                del self._buffer[:1]

    def _find_sync(self, start: int) -> int | None:
        buf = self._buffer
        end = len(buf) - 2
        i = start
        while i < end:
            if buf[i] == 0xAA and buf[i + 1] == 0xAA and buf[i + 2] == 0xAA:
                return i
            i += 1
        return None

    def _consume_csv_frame(self) -> bool:
        """CSV: 3 sync + 1 CNT + 67 data + 2 checksum + 2 CRLF = 75 bytes."""
        length = self._buffer[3]
        total = 3 + 1 + length + 2 + 2
        if len(self._buffer) < total:
            return False
        payload = bytes(self._buffer[4:4 + length])
        del self._buffer[:total]
        try:
            line = payload.decode("ascii", errors="replace")
            state = parse_status(line)
            if state:
                # Stamp the configured temperature unit onto every state.
                # The protocol doesn't tell us C vs F, so without this the
                # web client would have to assume one.
                state.temperature_unit = self.temperature_unit
                state.tune_active = self._last_tune_active
                # Diagnostic: log every op_status transition with the raw
                # decoded line + parsed (op,tx). Helps tell apart:
                #   - amp legitimately blipping to Stby (firmware quirk
                #     during band/ant switch / ATU tune / cmd ack);
                #   - RF-corrupted FTDI bytes producing fake Stby during
                #     high-power TX (the leading suspect for "continuous
                #     STANDBY flash during TX");
                #   - Node-RED or another WS client toggling oper/stby.
                # WARNING level so it surfaces in journald without DEBUG.
                if state.op_status != self._prev_op_status:
                    logger.warning(
                        f"op_status transition: "
                        f"{self._prev_op_status or '(init)'} -> {state.op_status} "
                        f"(tx={state.tx_status}) raw={line!r}"
                    )
                    self._prev_op_status = state.op_status

                # p_out averaging. Reset (don't smooth) across an op/tx
                # transition — e.g. RX -> TX at the start of a transmission
                # — so the average doesn't start a new TX dragged down by
                # whatever was reading during idle/standby.
                pout_key = (state.op_status, state.tx_status)
                try:
                    pout_raw = float(state.p_out)
                except (TypeError, ValueError):
                    pout_raw = 0.0
                if self._pout_avg is None or pout_key != self._pout_avg_key:
                    self._pout_avg = pout_raw
                else:
                    a = self._pout_avg_alpha
                    self._pout_avg = a * pout_raw + (1 - a) * self._pout_avg
                self._pout_avg_key = pout_key
                state.p_out_avg = round(self._pout_avg, 1)

                # p_out peak-hold. Reset on the same op/tx transitions as
                # the average, for the same reason — a fresh TX shouldn't
                # start by holding whatever the amp last read during idle.
                now = time.monotonic()
                if pout_key != self._pout_peak_key:
                    # New TX/RX context — start the hold fresh at the live
                    # reading.
                    self._pout_peak = pout_raw
                    self._pout_peak_at = now
                elif pout_raw >= self._pout_peak:
                    # New high sample — pin it and restart the hold timer.
                    self._pout_peak = pout_raw
                    self._pout_peak_at = now
                elif (now - self._pout_peak_at) > self._pout_peak_hold_s:
                    # Hold window elapsed — decay toward the live reading at
                    # a constant W/s, scaled by real elapsed time since the
                    # previous frame so the rate is independent of the
                    # sample rate. The max() floor stops it dropping below
                    # the live reading.
                    dt = now - self._pout_peak_prev_tick
                    self._pout_peak = max(
                        pout_raw,
                        self._pout_peak - self._pout_peak_decay_w_per_s * dt,
                    )
                self._pout_peak_prev_tick = now
                self._pout_peak_key = pout_key
                state.p_out_peak = round(self._pout_peak, 1)

                self._state = state
                self._last_state_at = time.monotonic()
                self.on_state_update(state)
        except Exception as e:
            logger.warning(f"CSV parse failed: {e}")
        return True

    def _consume_rcu_frame(self) -> bool:
        """RCU: sync + 0x6A + payload; payload ends at next sync."""
        next_sync = self._find_sync(4)
        if next_sync is None:
            return False
        payload = bytes(self._buffer[4:next_sync])
        del self._buffer[:next_sync]
        self._emit_rcu_frame(payload)
        return True

    def _flush_open_rcu_frame(self) -> None:
        if len(self._buffer) < 4:
            return
        if not (
            self._buffer[0] == 0xAA
            and self._buffer[1] == 0xAA
            and self._buffer[2] == 0xAA
            and self._buffer[3] == RESP_RCU_TYPE
        ):
            return
        payload = bytes(self._buffer[4:])
        self._buffer.clear()
        self._emit_rcu_frame(payload)

    def _emit_rcu_frame(self, payload: bytes) -> None:
        # Stamp liveness unconditionally — even with no RCU consumer
        # subscribed, an arrived frame still proves the amp is alive.
        self._last_rcu_at = time.monotonic()
        # Track the TUNE LED state directly from the RCU LCD frame —
        # byte 4 bit 6 (mask 0x40). CLEAR = LED on (amp is in TUNE
        # mode, covers both "waiting for carrier" and "ATU sweeping").
        # SET = LED off. Identified 2026-06-19 via labelled-diff
        # capture; see macexpert-spe docs/REVERSE_ENGINEERING.md
        # "Hunting status flags". The Phase-2 tune orchestrator polls
        # ``last_tune_active`` to decide when to start / stop the
        # Flex carrier; the state JSON ``tune_active`` field
        # surfaces it to all WS clients.
        if len(payload) > 4:
            self._last_tune_active = (payload[4] & 0x40) == 0
        if not self.on_rcu_frame:
            return
        try:
            self.on_rcu_frame(payload)
        except Exception as e:
            logger.warning(f"RCU frame emit failed: {e}")
