"""Single-frequency ATU tune orchestrator.

Coordinates the three moving parts of an SM5TOG-style tune cycle on
the SPE 1.5K-FA:

  1. SerialHandler — send the SPE TUNE keycode (CMD_TUNE = 0x09),
     read the live ``last_tune_active`` flag (byte 4 bit 6 of each
     incoming RCU frame, set CLEAR when the front-panel TUNE LED is
     lit) to detect tune entry and ATU completion.
  2. FlexConnection — set slice freq and tune power, key the built-in
     tune carrier on / off.
  3. status callback — relay phase transitions out to WS clients so
     MacExpert / the browser dashboard can render progress.

Phase 2a scope: a single cycle on the Flex's current freq (or an
optional override). Phase 2b will wrap this in a band-sweep loop.

Design notes:

  * No blind timing: the only timeouts are *safety* timeouts (refuse
    to wait forever if the LED never lights or the ATU never finishes).
    Steady-state progress is driven by the LED bit, not the clock.
  * Cleanup-first: the carrier-off command runs in a finally block so
    a crashed or cancelled orchestrator can't leave the rig in TX.
  * No interlock create dance: firmware 1.4.0.0 on the test rig
    rejected the ethernet-interlock commands, and direct
    ``transmit tune on`` was accepted without needing the dance.
    Re-evaluate when newer firmware is in play.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from spe.radio import RadioConnection
from spe.radio_controller import RadioController
from spe.serial_handler import SerialHandler
from spe.spe_band_table import BAND_TABLE, lookup as lookup_band

logger = logging.getLogger(__name__)

# Time the amp gets to acknowledge our TUNE keycode by lighting the
# LED (byte 4 bit 6 → CLEAR). The RCU tick interval is 0.5 s, so the
# worst-case latency for the bit to update on our side is one tick;
# 2 s gives us four tick windows of slack, which is plenty.
TUNE_ENTRY_TIMEOUT = 2.0

# Max time we let the ATU sweep before declaring something's wrong
# and aborting. The 1.5K-FA's ATU completes in 2-4 s typically; 10 s
# is generous but still bounded so a hung amp doesn't strand the
# carrier on the antenna.
TUNE_SWEEP_TIMEOUT = 10.0

# How often we poll ``serial.last_tune_active`` during the wait
# loops. RCU frames arrive at 0.5 s ticks; polling at 0.1 s means
# we observe each transition within ~100 ms of the next RCU update.
_POLL_INTERVAL = 0.1


# Status phases emitted via on_status. The orchestrator always ends
# in one of SUCCESS, FAIL, or ABORT — those are the terminal states
# clients can latch on to.
PHASES = (
    "STARTED",         # cycle accepted; preflight begins
    "PREFLIGHT_OK",    # amp in STBY, carrier off, ready to send TUNE
    "VFO_SAVED",       # operator's freq+mode snapshotted before any change
    "FREQ_SET",        # Flex slice tuned to target freq (only if override)
    "TUNE_SENT",       # SPE TUNE keycode written
    "LED_ON",          # SPE confirmed TUNE entry (byte 4 bit 6 CLEAR)
    "CARRIER_ON",      # Flex tune carrier on; ATU should now sweep
    "LED_OFF",         # SPE LED off — ATU done (or aborted internally)
    "CARRIER_OFF",     # Flex carrier stopped
    "VFO_RESTORED",    # operator's saved freq+mode written back
    "SUCCESS",         # terminal: single cycle completed cleanly
    "FAIL",            # terminal: error during the cycle (message has why)
    "ABORT",           # terminal: external stop() while running
    # Band-sweep phases — emitted in addition to the per-cycle phases
    # above when tune_band() is running.
    "SWEEP_STARTED",   # band sweep accepted; first sub-band about to start
    "SWEEP_STEP",      # next sub-band's tune cycle is about to begin
    "SWEEP_DONE",      # terminal: all sub-bands tuned cleanly
    # Radio connection-lifecycle phases — emitted by RadioController on the
    # same channel as the connection is opened on Sweep-menu open / tune
    # start and closed when the cycle is over. Listed here so clients have
    # the full phase vocabulary in one place.
    "RADIO_CONNECTING",
    "RADIO_CONNECTED",
    "RADIO_DISCONNECTED",
    "RADIO_ERROR",
)


StatusCallback = Callable[[str, str], None]


class TuneOrchestrator:
    """Drives one ATU tune cycle end-to-end.

    Instances are reusable — call ``tune_single()`` repeatedly. The
    orchestrator guards against concurrent cycles internally (a second
    call while one is running emits FAIL "Tune already in progress").
    """

    def __init__(
        self,
        serial_handler: SerialHandler,
        radio_controller: RadioController,
        on_status: Optional[StatusCallback] = None,
    ):
        self.serial = serial_handler
        # The connection is opened on demand via the controller (see
        # _acquire_radio / _release_radio), not held for the server's life.
        # The controller also knows the active backend, channel, and power.
        self.radio_controller = radio_controller
        self.on_status = on_status

        self._running = False
        self._stop_requested = asyncio.Event()

    async def _acquire_radio(self) -> Optional[RadioConnection]:
        """Open (or reuse) the radio connection for a tune cycle.

        Returns the live connection, or None if it couldn't be
        established — in which case RadioController has already emitted a
        RADIO_ERROR status, and the caller should emit FAIL and bail."""
        return await self.radio_controller.connect()

    async def _release_radio(self) -> None:
        """Drop the radio connection now the cycle is over. Best effort."""
        await self.radio_controller.disconnect()

    def _status(self, phase: str, message: str = "") -> None:
        """Emit a phase transition. Internal logging at INFO; the
        external callback gets phase + a human-readable message."""
        logger.info(f"Tune[{phase}] {message}".rstrip())
        cb = self.on_status
        if cb is not None:
            try:
                cb(phase, message)
            except Exception:
                logger.exception("Tune on_status callback raised")

    async def tune_single(self, freq_mhz: Optional[float] = None) -> bool:
        """Run a single tune cycle. Returns True on SUCCESS, else False.

        ``freq_mhz`` overrides the radio's frequency before keying. The
        operator's pre-call freq + mode are snapshotted and restored after
        the cycle either way (the cycle sets the channel to CW). Omit
        ``freq_mhz`` to tune at the radio's current freq.
        """
        if self._running:
            self._status("FAIL", "Tune already in progress")
            return False

        self._running = True
        self._stop_requested.clear()
        try:
            radio = await self._acquire_radio()
            if radio is None:
                self._status("FAIL", "Radio not reachable")
                return False
            snap = self._snapshot(radio)
            try:
                return await self._run_one_cycle(radio, freq_mhz)
            finally:
                await self._restore(radio, snap)
        finally:
            # Disconnect once the cycle is over, per the on-demand
            # lifecycle — the radio is only held while actually tuning.
            await self._release_radio()
            self._running = False

    async def tune_band(self, band: str) -> bool:
        """Sweep the SPE manual's recommended sub-band central frequencies
        for ``band`` (e.g. "20m", "40m"). For each sub-band, sets the
        Flex slice freq + runs a full tune cycle. The operator is
        responsible for picking the band and antenna *before* calling
        — spe-remote does not change either.

        Returns True on SUCCESS (every sub-band cycle succeeded). False
        if any sub-band failed or the sweep was stopped — the per-cycle
        failure phase tells the client which sub-band gave up.
        """
        if self._running:
            self._status("FAIL", "Tune already in progress")
            return False

        try:
            centers_khz = lookup_band(band)
            raw_total = len(BAND_TABLE.get(band.strip().lower().rstrip("m") + "m",
                                            BAND_TABLE.get(band.strip().lower(), [])))
        except KeyError as e:
            self._status("FAIL", str(e))
            return False

        if not centers_khz:
            self._status("FAIL", f"{band}: no in-band sub-bands to sweep")
            return False

        self._running = True
        self._stop_requested.clear()
        radio = None
        snap = None
        try:
            radio = await self._acquire_radio()
            if radio is None:
                self._status("FAIL", "Radio not reachable")
                return False
            snap = self._snapshot(radio)
            total = len(centers_khz)
            skipped = raw_total - total
            note = (f" ({skipped} out-of-band entries from the manual skipped)"
                    if skipped > 0 else "")
            self._status(
                "SWEEP_STARTED",
                f"{band}: {total} sub-bands "
                f"({centers_khz[0]/1000:.3f}–{centers_khz[-1]/1000:.3f} MHz)"
                + note
            )

            completed = 0
            for i, center_khz in enumerate(centers_khz, start=1):
                if self._stop_requested.is_set():
                    self._status("ABORT",
                                 f"stopped at sub-band {i}/{total}")
                    return False

                freq_mhz = center_khz / 1000.0
                self._status("SWEEP_STEP",
                             f"{i}/{total}: {freq_mhz:.4f} MHz")

                ok = await self._run_one_cycle(radio, freq_mhz)
                if not ok:
                    # _run_one_cycle has already emitted FAIL with the
                    # specific reason — surface a sweep-level summary
                    # so clients can latch on it.
                    self._status("FAIL",
                                 f"sub-band {i}/{total} failed at "
                                 f"{freq_mhz:.4f} MHz; sweep aborted")
                    return False

                completed += 1
                # Brief pause between cycles — SM5TOG's PAUSE_STEP.
                # Long enough for ATU relays to settle and serial
                # buffer to drain before the next freq command.
                await asyncio.sleep(1.0)

            self._status("SWEEP_DONE",
                         f"{completed}/{total} sub-bands tuned on {band}")
            return True

        finally:
            # Restore the operator's pre-sweep VFO + mode before we
            # release _running. Best effort — log on failure but don't
            # mask whatever terminal phase the sweep produced.
            if radio is not None:
                await self._restore(radio, snap)
            # Disconnect now the sweep is over (on-demand lifecycle).
            await self._release_radio()
            self._running = False

    async def _run_one_cycle(self, radio: RadioConnection,
                             freq_mhz: Optional[float]) -> bool:
        """Single ATU tune cycle. Used by both tune_single (one call)
        and tune_band (called N times in a loop). Caller is responsible
        for setting / clearing self._running around this method.

        Drives the active radio through the generic RadioConnection
        interface, so the same sequence works for a Flex (SmartSDR) or a
        SunSDR (TCI) rig.
        """
        channel = self.radio_controller.channel
        power = self.radio_controller.tune_power_watts
        carrier_on = False
        success = False

        try:
            # ----- Preflight ----------------------------------------
            self._status("STARTED",
                         f"freq={freq_mhz}" if freq_mhz else "(current freq)")

            if self.serial.state.op_status != "Stby":
                self._status("FAIL", "SPE must be in STBY to tune (currently "
                             f"{self.serial.state.op_status!r})")
                return False

            if self.serial.last_tune_active:
                # Someone else already pressed TUNE on the front panel
                # — refuse to overlap. The cycle would still likely
                # work but the abort semantics get confusing.
                self._status("FAIL", "SPE TUNE LED already on; refusing to "
                             "stack a second cycle on top")
                return False

            self._status("PREFLIGHT_OK")

            # ----- Freq + mode + power setup ------------------------
            try:
                if freq_mhz is not None:
                    await radio.set_frequency(channel, freq_mhz)
                    self._status("FREQ_SET",
                                 f"channel {channel} → {freq_mhz:.6f} MHz")
                # Key a clean CW carrier on the tuned channel.
                await radio.set_mode(channel, "CW")
                await radio.set_tune_power(power)
            except Exception as e:
                self._status("FAIL", f"radio setup: {e}")
                return False

            # ----- Send TUNE keycode, wait for LED ------------------
            self.serial.send_command("tune")
            self._status("TUNE_SENT", f"waiting up to {TUNE_ENTRY_TIMEOUT}s "
                         "for SPE TUNE LED")

            if not await self._wait_for_tune_active(True, TUNE_ENTRY_TIMEOUT):
                self._status("FAIL", "SPE didn't enter TUNE mode within "
                             f"{TUNE_ENTRY_TIMEOUT}s")
                return False

            self._status("LED_ON")

            # ----- Carrier on, wait for ATU done --------------------
            try:
                await radio.tune_carrier(on=True)
            except Exception as e:
                self._status("FAIL", f"tune_carrier(on): {e}")
                return False
            carrier_on = True
            self._status("CARRIER_ON",
                         f"{self.radio_controller.kind} carrier"
                         + (f" {power}W" if power else ""))

            if not await self._wait_for_tune_active(False, TUNE_SWEEP_TIMEOUT):
                self._status("FAIL", "ATU didn't complete within "
                             f"{TUNE_SWEEP_TIMEOUT}s — aborting")
                return False

            self._status("LED_OFF", "ATU done")

            success = True
            return True

        except asyncio.CancelledError:
            self._status("ABORT", "cancelled")
            return False
        except Exception as e:
            logger.exception("Tune orchestrator crashed")
            self._status("FAIL", f"internal error: {e}")
            return False
        finally:
            # Carrier off MUST run regardless of how we got here —
            # the carrier is the only thing that can hurt antennas /
            # the amp if left on. Tolerate the off failing (best
            # effort); the radio's own watchdog will cut TX eventually
            # if all else fails.
            if carrier_on:
                try:
                    await radio.tune_carrier(on=False)
                    self._status("CARRIER_OFF")
                except Exception:
                    logger.exception("Failed to stop carrier in cleanup")
                    self._status("FAIL", "carrier-off failed in cleanup")

            self._status("SUCCESS" if success else "FAIL",
                         "cycle complete" if success else "see prior status")

    def _snapshot(self, radio: RadioConnection) -> Optional[dict]:
        """Capture the operator's current freq+mode via the radio backend
        so it can be restored after the cycle. Returns an opaque dict for
        :meth:`_restore`, or None if the backend doesn't know the state
        yet (restore is then skipped). Emits ``VFO_SAVED``."""
        channel = self.radio_controller.channel
        snap = radio.snapshot(channel)
        if snap is None:
            self._status("VFO_SAVED",
                         f"channel {channel} state unknown — restore disabled")
            return None
        self._status("VFO_SAVED",
                     f"channel {channel}: {snap.get('freq')} {snap.get('mode')}")
        return snap

    async def _restore(self, radio: RadioConnection,
                       snap: Optional[dict]) -> None:
        """Write a :meth:`_snapshot` result back via the radio backend.
        Best effort — never re-raises (the cycle already has its own
        terminal phase queued)."""
        if snap is None:
            return
        try:
            await radio.restore(snap)
            self._status("VFO_RESTORED",
                         f"channel {snap.get('channel')}: "
                         f"{snap.get('freq')} {snap.get('mode')}")
        except Exception as e:
            logger.exception("Failed to restore radio freq+mode")
            self._status("FAIL", f"VFO restore: {e}")

    async def _wait_for_tune_active(self, expected: bool, timeout: float) -> bool:
        """Poll ``serial.last_tune_active`` until it equals ``expected``
        or ``timeout`` elapses. Returns True on match. Raises
        asyncio.CancelledError if stop() was requested."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._stop_requested.is_set():
                raise asyncio.CancelledError()
            if self.serial.last_tune_active == expected:
                return True
            await asyncio.sleep(_POLL_INTERVAL)
        return False

    def stop(self) -> None:
        """Request an immediate abort of the running cycle.

        The async ``tune_single`` will see the flag in its next poll
        and raise CancelledError, triggering the finally block (which
        guarantees carrier-off). Safe to call from any thread or
        coroutine."""
        self._stop_requested.set()

    @property
    def is_running(self) -> bool:
        return self._running
