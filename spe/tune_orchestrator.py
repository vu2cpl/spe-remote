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
  * Leave the amp how we found it: tune_single/tune_band drop the amp
    to STBY themselves (CMD_OPERATE toggle, verified via CSV
    op_status) and hand OPERATE back at the end iff it was on at the
    start. The per-cycle preflight still hard-checks STBY as a safety
    net against mid-sweep front-panel flips.
  * The radio rules the band: tune_band maps the radio's slice freq
    to its ham band and sweeps THAT band — a menu pick for a
    different band is overridden with a note, never refused
    (operator's call: the antenna follows the radio, so the radio's
    band is always the safe one to tune). The explicit pick is only
    used when the radio's band can't be read. Antenna selection
    itself stays with the operator — spe-remote never touches it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from spe.config import FlexConfig
from spe.flex import FlexConnection, FlexProtocolError
from spe.flex_controller import FlexController
from spe.serial_handler import SerialHandler
from spe.spe_band_table import BAND_TABLE, band_for_freq, lookup as lookup_band

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

# Time the amp gets to reflect an OPERATE↔STBY toggle (CMD_OPERATE
# keycode) in its CSV op_status. Idle CSV polling runs at ~1 s
# intervals, so 4 s covers several status frames of slack.
OPER_SWITCH_TIMEOUT = 4.0

# Time we allow the Flex's post-connect ``sub slice all`` events to
# populate the slice cache before the band check gives up on reading
# the radio's freq. Slice status normally arrives within tens of ms
# of the subscription; 2 s is generous.
SLICE_STATE_TIMEOUT = 2.0


# Status phases emitted via on_status. The orchestrator always ends
# in one of SUCCESS, FAIL, or ABORT — those are the terminal states
# clients can latch on to.
PHASES = (
    "STARTED",         # cycle accepted; preflight begins
    "PREFLIGHT_OK",    # amp in STBY, carrier off, ready to send TUNE
    "STBY_SET",        # amp confirmed in STBY (auto-switched from OPERATE
                       # if needed, remembering the prior mode)
    "OPER_RESTORED",   # amp handed back to OPERATE at the end (only when
                       # it was in OPERATE at the start)
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
    "BAND_CHECKED",    # sweep band resolved from the radio's slice freq
                       # (radio rules; the explicit pick is used only when
                       # the radio's band can't be read)
    "SWEEP_STARTED",   # band sweep accepted; first sub-band about to start
    "SWEEP_STEP",      # next sub-band's tune cycle is about to begin
    "SWEEP_DONE",      # terminal: all sub-bands tuned cleanly
    # Flex connection-lifecycle phases — emitted by FlexController on the
    # same channel as the connection is opened on Sweep-menu open / tune
    # start and closed when the cycle is over. Listed here so clients have
    # the full phase vocabulary in one place.
    "FLEX_CONNECTING",
    "FLEX_CONNECTED",
    "FLEX_DISCONNECTED",
    "FLEX_ERROR",
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
        flex_controller: FlexController,
        config: FlexConfig,
        on_status: Optional[StatusCallback] = None,
    ):
        self.serial = serial_handler
        # The connection is opened on demand via the controller (see
        # _acquire_flex / _release_flex), not held for the server's life.
        self.flex_controller = flex_controller
        self.config = config
        self.on_status = on_status

        self._running = False
        self._stop_requested = asyncio.Event()

    async def _acquire_flex(self) -> Optional[FlexConnection]:
        """Open (or reuse) the Flex connection for a tune cycle.

        Returns the live connection, or None if it couldn't be
        established — in which case FlexController has already emitted a
        FLEX_ERROR status, and the caller should emit FAIL and bail."""
        return await self.flex_controller.connect()

    async def _release_flex(self) -> None:
        """Drop the Flex connection now the cycle is over. Best effort."""
        await self.flex_controller.disconnect()

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

        ``freq_mhz`` overrides the Flex slice frequency before keying;
        the operator's pre-call freq + mode are snapshotted and
        restored after the cycle. Omit ``freq_mhz`` to tune at whatever
        freq the slice is already on (no save/restore needed in that
        case — the slice didn't move).

        The amp is auto-switched to STBY for the cycle and handed back
        to OPERATE at the end iff it was in OPERATE at the start.
        """
        if self._running:
            self._status("FAIL", "Tune already in progress")
            return False

        self._running = True
        self._stop_requested.clear()
        was_operate = False
        try:
            flex = await self._acquire_flex()
            if flex is None:
                self._status("FAIL", "Flex radio not reachable")
                return False
            snap = self._snapshot_slice(flex) if freq_mhz is not None else None
            try:
                stby = await self._ensure_stby()
                if stby is None:
                    return False
                was_operate = stby
                return await self._run_one_cycle(flex, freq_mhz)
            finally:
                if snap is not None:
                    await self._restore_slice(flex, snap)
        except asyncio.CancelledError:
            # stop() while waiting for the STBY switch — _run_one_cycle
            # catches its own cancellations, so this only fires outside
            # it. The finally below still restores OPERATE if owed.
            self._status("ABORT", "cancelled")
            return False
        finally:
            if was_operate:
                await self._restore_operate()
            # Disconnect once the cycle is over, per the on-demand
            # lifecycle — the radio is only held while actually tuning.
            await self._release_flex()
            self._running = False

    async def tune_band(self, band: str = "") -> bool:
        """Sweep the SPE manual's recommended sub-band central frequencies
        for ``band`` (e.g. "20m", "40m"). For each sub-band, sets the
        Flex slice freq + runs a full tune cycle.

        **The radio rules the band**: the operator slice's freq is
        mapped to its ham band and that band is swept — ``band`` is
        overridden with a note if it disagrees, and only trusted when
        the radio's band can't be read ("" / "auto" / "current" with
        an unreadable radio FAILs). The amp is auto-switched to STBY
        for the sweep and handed back to OPERATE at the end iff it
        was in OPERATE at the start. Antenna selection stays with the
        operator — spe-remote never touches it.

        Returns True on SUCCESS (every sub-band cycle succeeded). False
        if any sub-band failed or the sweep was stopped — the per-cycle
        failure phase tells the client which sub-band gave up.
        """
        if self._running:
            self._status("FAIL", "Tune already in progress")
            return False

        self._running = True
        self._stop_requested.clear()
        flex = None
        snap = None
        was_operate = False
        try:
            flex = await self._acquire_flex()
            if flex is None:
                self._status("FAIL", "Flex radio not reachable")
                return False

            band = await self._resolve_sweep_band(flex, band)
            if band is None:
                return False

            # Band name is validated by _resolve_sweep_band, so these
            # lookups can't raise.
            centers_khz = lookup_band(band)
            if not centers_khz:
                self._status("FAIL", f"{band}: no in-band sub-bands to sweep")
                return False
            raw_total = len(lookup_band(band, in_band_only=False))

            snap = self._snapshot_slice(flex)

            stby = await self._ensure_stby()
            if stby is None:
                return False
            was_operate = stby

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

                ok = await self._run_one_cycle(flex, freq_mhz)
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

        except asyncio.CancelledError:
            # stop() during band check / STBY switch — the per-cycle
            # code catches its own cancellations, so this only fires
            # outside _run_one_cycle. The finally below still restores
            # the VFO and OPERATE if owed.
            self._status("ABORT", "cancelled")
            return False
        finally:
            # Restore the operator's pre-sweep VFO + mode before we
            # release _running. Best effort — log on failure but don't
            # mask whatever terminal phase the sweep produced.
            if flex is not None and snap is not None:
                await self._restore_slice(flex, snap)
            # Hand the amp back to OPERATE last, after the carrier is
            # guaranteed off (per-cycle finally) and the VFO is back.
            if was_operate:
                await self._restore_operate()
            # Disconnect now the sweep is over (on-demand lifecycle).
            await self._release_flex()
            self._running = False

    async def _run_one_cycle(self, flex: FlexConnection,
                             freq_mhz: Optional[float]) -> bool:
        """Single ATU tune cycle. Used by both tune_single (one call)
        and tune_band (called N times in a loop). Caller is responsible
        for setting / clearing self._running around this method.
        """
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

            # ----- Optional freq + power setup ----------------------
            if freq_mhz is not None:
                try:
                    await flex.set_slice_freq(self.config.slice_rx, freq_mhz)
                except FlexProtocolError as e:
                    self._status("FAIL", f"set_slice_freq: {e}")
                    return False
                self._status("FREQ_SET", f"slice {self.config.slice_rx} → "
                             f"{freq_mhz:.6f} MHz")

            try:
                await flex.set_tune_power(self.config.tune_power_watts)
            except FlexProtocolError as e:
                self._status("FAIL", f"set_tune_power: {e}")
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
                await flex.tune_carrier(on=True)
            except FlexProtocolError as e:
                self._status("FAIL", f"tune_carrier(on): {e}")
                return False
            carrier_on = True
            self._status("CARRIER_ON",
                         f"Flex {self.config.tune_power_watts}W")

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
            # effort); the FlexConnection's own reconnect will sort
            # things out and the rig's own watchdog will cut TX
            # eventually if all else fails.
            if carrier_on:
                try:
                    await flex.tune_carrier(on=False)
                    self._status("CARRIER_OFF")
                except Exception:
                    logger.exception("Failed to stop carrier in cleanup")
                    self._status("FAIL", "carrier-off failed in cleanup")

            self._status("SUCCESS" if success else "FAIL",
                         "cycle complete" if success else "see prior status")

    def _snapshot_slice(self, flex: FlexConnection) -> Optional[dict]:
        """Read the current freq+mode of the operator's slice from
        FlexConnection.slice_state. Returns a small dict the
        orchestrator can hand to ``_restore_slice`` later, or None if
        the cache isn't populated yet (e.g. the radio hasn't emitted a
        slice event since spe-remote connected). Emits ``VFO_SAVED`` on
        success."""
        rx = self.config.slice_rx
        state = flex.slice_state.get(rx)
        if not state:
            self._status("VFO_SAVED",
                         f"slice {rx} state unknown — restore disabled")
            return None
        freq = state.get("RF_frequency")
        mode = state.get("mode")
        if freq is None and mode is None:
            self._status("VFO_SAVED",
                         f"slice {rx} state empty — restore disabled")
            return None
        self._status("VFO_SAVED",
                     f"slice {rx}: {freq} MHz {mode}")
        return {"rx": rx, "freq": freq, "mode": mode}

    async def _restore_slice(self, flex: FlexConnection,
                             snap: Optional[dict]) -> None:
        """Write the saved freq+mode back to the Flex slice. Best
        effort — any failure logs at WARN and a FAIL status is emitted,
        but we never re-raise (the cycle that called us already has
        its own terminal phase queued)."""
        if snap is None:
            return
        rx = snap["rx"]
        freq = snap["freq"]
        mode = snap["mode"]
        try:
            if freq is not None:
                await flex.set_slice_freq(rx, float(freq))
            if mode is not None:
                await flex.set_slice_mode(rx, mode)
            self._status("VFO_RESTORED",
                         f"slice {rx}: {freq} MHz {mode}")
        except Exception as e:
            logger.exception("Failed to restore slice freq+mode")
            self._status("FAIL", f"VFO restore: {e}")

    async def _resolve_sweep_band(self, flex: FlexConnection,
                                  requested: str) -> Optional[str]:
        """Pick the band to sweep — **the radio rules** (operator's
        design call, 2026-09-02, superseding the first-cut
        mismatch-FAIL behaviour): whenever the operator slice's freq
        maps to a ham band, that band is swept, and a menu pick for a
        different band is overridden with a note rather than refused.
        The antenna hanging off the amp is the one selected for the
        radio's band, so following the radio is always the safe
        choice — and the operator never has to fix a stale menu pick
        before tuning.

        The explicit ``requested`` band only matters when the radio's
        band can't be determined (no slice data, or freq outside every
        ham band): then it is trusted with a note. "" / "auto" /
        "current" with an unreadable radio FAILs (nothing to sweep).

        Returns the band name to sweep, or None after emitting FAIL."""
        radio_freq = await self._wait_for_slice_freq(flex)
        radio_band = (band_for_freq(radio_freq)
                      if radio_freq is not None else None)
        amp_band = self.serial.state.band
        req = requested.strip().lower()

        if radio_band is not None:
            note = ""
            key = req if req in BAND_TABLE else req + "m"
            if req not in ("", "auto", "current") and key != radio_band:
                note = f" — overriding the requested {requested.strip()}"
            if amp_band in BAND_TABLE and amp_band != radio_band:
                # Informational only — the SPE counts the exciter freq
                # and switches band itself once the carrier appears.
                note += (f" (amp currently shows {amp_band}; it follows "
                         "the exciter freq once TUNE starts)")
            self._status("BAND_CHECKED",
                         f"radio rules: on {radio_band} "
                         f"({radio_freq:.4f} MHz) — sweeping {radio_band}"
                         + note)
            return radio_band

        # Radio band unknown — fall back to the explicit request.
        why = (f"slice {self.config.slice_rx} at {radio_freq:.4f} MHz "
               "is outside every ham band"
               if radio_freq is not None else
               f"slice {self.config.slice_rx} state not available")
        if req in ("", "auto", "current"):
            self._status("FAIL",
                         f"can't derive the band to sweep — {why}; "
                         "pass an explicit band instead")
            return None
        # Normalize the same way lookup() does ("20" → "20m").
        key = req if req in BAND_TABLE else req + "m"
        if key not in BAND_TABLE:
            self._status("FAIL", f"Unknown band {requested!r}; "
                         f"known: {sorted(BAND_TABLE)}")
            return None
        self._status("BAND_CHECKED",
                     f"radio band unknown ({why}) — "
                     f"trusting the requested {key}")
        return key

    async def _wait_for_slice_freq(self, flex: FlexConnection
                                   ) -> Optional[float]:
        """Return the operator slice's current freq (MHz), waiting up
        to SLICE_STATE_TIMEOUT for the post-connect ``sub slice all``
        events to populate the cache. None if it never shows up (or
        doesn't parse) — callers treat that as 'radio band unknown'."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + SLICE_STATE_TIMEOUT
        while loop.time() < deadline:
            if self._stop_requested.is_set():
                return None
            state = flex.slice_state.get(self.config.slice_rx)
            freq = state.get("RF_frequency") if state else None
            if freq is not None:
                try:
                    return float(freq)
                except ValueError:
                    return None
            await asyncio.sleep(_POLL_INTERVAL)
        return None

    async def _ensure_stby(self) -> Optional[bool]:
        """Put the amp in STBY before tuning, remembering whether it
        was in OPERATE so the caller can hand OPERATE back afterwards.

        Returns True if the amp was in OPERATE and we toggled it to
        STBY, False if it was already in STBY (nothing to restore
        later), or None if the toggle didn't take (FAIL emitted —
        caller should bail).

        CMD_OPERATE is a *toggle*, not an absolute set, so the result
        is verified from the CSV op_status rather than assumed."""
        if self.serial.state.op_status != "Oper":
            self._status("STBY_SET", "amp already in STBY")
            return False
        self.serial.send_command("oper")
        if not await self._wait_for_op_status("Stby", OPER_SWITCH_TIMEOUT):
            self._status("FAIL",
                         "amp didn't drop to STBY within "
                         f"{OPER_SWITCH_TIMEOUT}s of the OPERATE toggle "
                         f"(still {self.serial.state.op_status!r})")
            return None
        self._status("STBY_SET", "amp switched OPERATE → STBY "
                     "(OPERATE will be restored when the tune is done)")
        return True

    async def _restore_operate(self) -> None:
        """Hand the amp back to OPERATE after a tune that auto-switched
        it to STBY. Cleanup-path best effort: never raises; emits
        OPER_RESTORED on success, FAIL if the amp won't switch back."""
        try:
            if self.serial.state.op_status == "Oper":
                # Operator (or front panel) already put it back.
                self._status("OPER_RESTORED", "amp already back in OPERATE")
                return
            self.serial.send_command("oper")
            if await self._wait_for_op_status("Oper", OPER_SWITCH_TIMEOUT,
                                              ignore_stop=True):
                self._status("OPER_RESTORED",
                             "amp restored STBY → OPERATE")
            else:
                self._status("FAIL",
                             "couldn't restore OPERATE — amp still "
                             f"{self.serial.state.op_status!r} after "
                             f"{OPER_SWITCH_TIMEOUT}s; check the front panel")
        except Exception as e:
            logger.exception("Failed to restore OPERATE")
            self._status("FAIL", f"OPERATE restore: {e}")

    async def _wait_for_op_status(self, expected: str, timeout: float,
                                  ignore_stop: bool = False) -> bool:
        """Poll ``serial.state.op_status`` until it equals ``expected``
        or ``timeout`` elapses. Returns True on match. Raises
        asyncio.CancelledError if stop() was requested, unless
        ``ignore_stop`` — the OPERATE-restore path runs in cleanup,
        where the stop flag is often still set from the abort that got
        us there."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if not ignore_stop and self._stop_requested.is_set():
                raise asyncio.CancelledError()
            if self.serial.state.op_status == expected:
                return True
            await asyncio.sleep(_POLL_INTERVAL)
        return False

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
