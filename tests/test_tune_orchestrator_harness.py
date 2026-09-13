"""Fake-driven harness for TuneOrchestrator TODO #44 changes.

Exercises: auto-STBY (+restore), band verification (match / mismatch /
auto / unknown), STBY-switch failure, stop-mid-sweep OPERATE restore,
and tune_single's STBY wrap. No hardware, no radio — FakeSerial and
FakeRadio simulate both ends. FakeRadio implements the generic
RadioConnection surface, so these cases cover any backend (Flex over
SmartSDR, SunSDR over TCI), not just the Flex.
"""
import asyncio
import sys
import types

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# spe.config imports pyyaml, which isn't installed Mac-side; the
# radio controller only uses these as type hints, so stub them out.
_fake_cfg = types.ModuleType("spe.config")
_fake_cfg.RadioConfig = object
_fake_cfg.FlexConfig = object
_fake_cfg.TciConfig = object
sys.modules["spe.config"] = _fake_cfg
# spe.serial_handler imports pyserial (also not installed Mac-side);
# the orchestrator only type-hints SerialHandler.
_fake_sh = types.ModuleType("spe.serial_handler")
_fake_sh.SerialHandler = object
sys.modules["spe.serial_handler"] = _fake_sh
# spe.tci reaches for tornado's websocket client (not installed
# Mac-side either); nothing here opens a real TCI socket.
_fake_ws = types.ModuleType("tornado.websocket")
_fake_ws.websocket_connect = None
_fake_ws.WebSocketClientConnection = object
_fake_tornado = types.ModuleType("tornado")
_fake_tornado.websocket = _fake_ws
sys.modules["tornado"] = _fake_tornado
sys.modules["tornado.websocket"] = _fake_ws

from spe.tune_orchestrator import TuneOrchestrator  # noqa: E402


class FakeState:
    def __init__(self, op_status="Stby", band="20m"):
        self.op_status = op_status
        self.band = band


class FakeSerial:
    """Simulates the amp: 'oper' toggles op_status after a short delay
    (like a CSV frame landing); 'tune' lights the TUNE LED."""

    def __init__(self, op_status="Stby", band="20m", oper_works=True):
        self.state = FakeState(op_status, band)
        self.last_tune_active = False
        self.oper_works = oper_works
        self.commands = []

    def send_command(self, command):
        self.commands.append(command)
        loop = asyncio.get_event_loop()
        if command == "oper" and self.oper_works:
            new = "Stby" if self.state.op_status == "Oper" else "Oper"
            loop.call_later(0.15, setattr, self.state, "op_status", new)
        elif command == "tune":
            loop.call_later(0.05, setattr, self, "last_tune_active", True)


class FakeRadio:
    """Implements the generic RadioConnection surface the orchestrator
    drives. ``state`` stands in for whatever cache a real backend fills
    from its own event stream (Flex slice events, TCI's startup dump);
    emptying it simulates a radio whose freq we can't read."""

    def __init__(self, serial, freq_mhz="14.074000", mode="USB"):
        self._serial = serial
        self.state = {0: {"freq": freq_mhz, "mode": mode}}
        self.calls = []

    async def set_frequency(self, channel, freq_mhz):
        self.calls.append(("freq", channel, freq_mhz))
        self.state.setdefault(channel, {})["freq"] = f"{freq_mhz:.6f}"

    async def set_mode(self, channel, mode):
        self.calls.append(("mode", channel, mode))

    async def set_tune_power(self, watts):
        self.calls.append(("tunepower", watts))

    async def tune_carrier(self, on):
        self.calls.append(("carrier", on))
        if on:
            # ATU finishes: LED goes off shortly after carrier appears
            asyncio.get_event_loop().call_later(
                0.1, setattr, self._serial, "last_tune_active", False)

    def snapshot(self, channel):
        st = self.state.get(channel)
        if not st:
            return None
        return {"channel": channel, "freq": st.get("freq"),
                "mode": st.get("mode")}

    async def restore(self, snap):
        if snap is None:
            return
        if snap.get("freq") is not None:
            await self.set_frequency(snap["channel"], float(snap["freq"]))
        if snap.get("mode") is not None:
            await self.set_mode(snap["channel"], snap["mode"])


class FakeController:
    """Stands in for RadioController: owns the connection and knows the
    active backend's kind, channel and tune power."""

    kind = "flex"
    channel = 0
    tune_power_watts = 10

    def __init__(self, radio):
        self._radio = radio

    async def connect(self):
        return self._radio

    async def disconnect(self):
        pass


def make(op_status="Stby", band="20m", freq="14.074000", oper_works=True,
         radio_none=False, mode="USB"):
    serial = FakeSerial(op_status, band, oper_works)
    radio = FakeRadio(serial, freq, mode)
    ctrl = FakeController(radio)
    if radio_none:
        async def _none():
            return None
        ctrl.connect = _none
    phases = []
    orch = TuneOrchestrator(serial, ctrl,
                            on_status=lambda p, m: phases.append((p, m)))
    return orch, serial, radio, phases


def names(phases):
    return [p for p, _ in phases]


FAILURES = []


def check(label, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


async def t1_sweep_operate_restore():
    """Sweep with amp in OPERATE: auto-STBY, sweep, OPERATE restored."""
    orch, serial, radio, phases = make(op_status="Oper")
    ok = await orch.tune_band("20m")
    ns = names(phases)
    check("t1 sweep succeeds", ok, str(phases))
    check("t1 BAND_CHECKED before STBY_SET",
          ns.index("BAND_CHECKED") < ns.index("STBY_SET"), str(ns))
    check("t1 STBY_SET emitted with toggle",
          any(p == "STBY_SET" and "OPERATE" in m for p, m in phases))
    check("t1 sweep done", "SWEEP_DONE" in ns)
    check("t1 VFO restored", "VFO_RESTORED" in ns)
    check("t1 OPER_RESTORED last-ish",
          ns.index("OPER_RESTORED") > ns.index("SWEEP_DONE"), str(ns))
    check("t1 amp back in Oper", serial.state.op_status == "Oper")
    check("t1 two oper toggles", serial.commands.count("oper") == 2,
          str(serial.commands))


async def t2_sweep_already_stby():
    """Amp already STBY: no toggle, no OPER_RESTORED."""
    orch, serial, radio, phases = make(op_status="Stby")
    ok = await orch.tune_band("20m")
    ns = names(phases)
    check("t2 sweep succeeds", ok, str(phases))
    check("t2 STBY_SET already", any(
        p == "STBY_SET" and "already" in m for p, m in phases))
    check("t2 no OPER_RESTORED", "OPER_RESTORED" not in ns, str(ns))
    check("t2 no oper toggle", "oper" not in serial.commands)
    check("t2 amp still Stby", serial.state.op_status == "Stby")


async def t3_radio_rules_override():
    """Radio on 20m, sweep requested 40m: radio rules — sweeps 20m."""
    orch, serial, radio, phases = make(op_status="Oper", freq="14.074000")
    ok = await orch.tune_band("40m")
    ns = names(phases)
    check("t3 sweep succeeds on radio band", ok, str(phases))
    check("t3 BAND_CHECKED says overriding",
          any(p == "BAND_CHECKED" and "overriding" in m and "20m" in m
              for p, m in phases), str(phases))
    check("t3 swept 20m freqs (not 40m)",
          all(14.0 < c[2] < 14.35 for c in radio.calls if c[0] == "freq"),
          str(radio.calls))
    check("t3 OPERATE restored", "OPER_RESTORED" in ns)


async def t4_auto_band():
    """tune_band('auto') derives 20m from the slice freq."""
    orch, serial, radio, phases = make(freq="14.200000")
    ok = await orch.tune_band("auto")
    check("t4 auto sweep succeeds", ok, str(phases))
    check("t4 BAND_CHECKED says sweeping it",
          any(p == "BAND_CHECKED" and "20m" in m for p, m in phases),
          str(phases))
    check("t4 swept 20m freqs",
          any(c[0] == "freq" and 14.0 < c[2] < 14.35 for c in radio.calls))


async def t5_stby_switch_fails():
    """Amp ignores the OPERATE toggle: FAIL, no tune."""
    orch, serial, radio, phases = make(op_status="Oper", oper_works=False)
    ok = await orch.tune_band("20m")
    check("t5 sweep refused", not ok)
    check("t5 FAIL mentions STBY",
          any(p == "FAIL" and "STBY" in m for p, m in phases), str(phases))
    check("t5 no tune sent", "tune" not in serial.commands)
    check("t5 no OPER_RESTORED (we never got to STBY)",
          "OPER_RESTORED" not in names(phases))


async def t6_stop_mid_sweep_restores_operate():
    """stop() mid-sweep: ABORT, and OPERATE still restored."""
    orch, serial, radio, phases = make(op_status="Oper")
    task = asyncio.ensure_future(orch.tune_band("20m"))
    # Wait until the first cycle is underway, then stop.
    while "CARRIER_ON" not in names(phases):
        await asyncio.sleep(0.02)
    orch.stop()
    ok = await task
    ns = names(phases)
    check("t6 sweep aborted", not ok)
    check("t6 ABORT emitted", "ABORT" in ns, str(ns))
    check("t6 OPER_RESTORED after abort", "OPER_RESTORED" in ns, str(ns))
    check("t6 amp back in Oper", serial.state.op_status == "Oper")
    check("t6 carrier off ran",
          radio.calls and ("carrier", False) in radio.calls)


async def t7_single_operate_restore():
    """tune_single with amp in OPERATE: STBY wrap + restore."""
    orch, serial, radio, phases = make(op_status="Oper")
    ok = await orch.tune_single()
    ns = names(phases)
    check("t7 single succeeds", ok, str(phases))
    check("t7 STBY_SET", "STBY_SET" in ns)
    check("t7 OPER_RESTORED", "OPER_RESTORED" in ns)
    check("t7 amp back in Oper", serial.state.op_status == "Oper")


async def t8_unknown_band():
    # Radio parked outside every ham band, so the explicit request is
    # what counts — and it's not a band the table knows.
    orch, serial, radio, phases = make(freq="9.500000")
    ok = await orch.tune_band("2m")
    check("t8 unknown band refused", not ok)
    check("t8 FAIL says unknown",
          any(p == "FAIL" and "Unknown band" in m for p, m in phases),
          str(phases))


async def t9_channel_unknown_trusts_request():
    """No radio state: explicit request trusted with a note (short wait)."""
    orch, serial, radio, phases = make(op_status="Stby")
    radio.state = {}
    ok = await orch.tune_band("20m")
    check("t9 sweep proceeds on trust", ok, str(phases))
    check("t9 BAND_CHECKED unknown note",
          any(p == "BAND_CHECKED" and "unknown" in m for p, m in phases),
          str(phases))


async def t11_digu_tunes_in_cw():
    """DIGU channel: mode flips to CW for the tune, DIGU restored after."""
    orch, serial, radio, phases = make(mode="DIGU")
    ok = await orch.tune_band("20m")
    check("t11 sweep succeeds", ok, str(phases))
    check("t11 MODE_SET emitted",
          any(p == "MODE_SET" and "CW" in m and "DIGU" in m
              for p, m in phases), str(phases))
    mode_calls = [c for c in radio.calls if c[0] == "mode"]
    check("t11 CW set then DIGU restored",
          mode_calls[:1] == [("mode", 0, "CW")] and
          mode_calls[-1] == ("mode", 0, "DIGU"), str(mode_calls))


async def t12_cw_mode_untouched():
    """Channel already CW: no MODE_SET, no mode commands."""
    orch, serial, radio, phases = make(mode="CW")
    ok = await orch.tune_band("20m")
    check("t12 sweep succeeds", ok, str(phases))
    check("t12 no MODE_SET", "MODE_SET" not in names(phases))
    check("t12 mode restore is CW (harmless)",
          all(c[2] == "CW" for c in radio.calls if c[0] == "mode"),
          str(radio.calls))


async def t13_restore_failure_is_not_vfo_restored():
    """A backend that raises from restore() must surface as FAIL, not a
    VFO_RESTORED the radio never honoured — the slice/TRX is still
    parked on the last swept sub-band. Both backends re-raise now
    (spe/tci.py and spe/flex.py), so this covers either."""
    orch, serial, radio, phases = make(op_status="Oper")

    async def dead_link(snap):
        raise ConnectionError("link dropped")
    radio.restore = dead_link

    await orch.tune_single()
    check("t13 no false VFO_RESTORED",
          "VFO_RESTORED" not in names(phases), str(names(phases)))
    check("t13 FAIL says it was the restore",
          any(p == "FAIL" and "VFO restore" in m for p, m in phases),
          str(phases))
    check("t13 amp still handed back to OPERATE",
          "OPER_RESTORED" in names(phases) and serial.state.op_status == "Oper",
          str(names(phases)))


async def t10_radio_unreachable():
    orch, serial, radio, phases = make(op_status="Oper", radio_none=True)
    ok = await orch.tune_band("20m")
    check("t10 refused", not ok)
    check("t10 no oper toggle", "oper" not in serial.commands)
    check("t10 amp untouched", serial.state.op_status == "Oper")


async def main():
    for t in (t1_sweep_operate_restore, t2_sweep_already_stby,
              t3_radio_rules_override, t4_auto_band, t5_stby_switch_fails,
              t6_stop_mid_sweep_restores_operate, t7_single_operate_restore,
              t8_unknown_band, t9_channel_unknown_trusts_request,
              t10_radio_unreachable, t11_digu_tunes_in_cw,
              t12_cw_mode_untouched, t13_restore_failure_is_not_vfo_restored):
        await t()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    # Shrink the inter-cycle pause so the full-sweep tests run fast.
    import spe.tune_orchestrator as to
    orig_sleep = asyncio.sleep

    async def fast_sleep(t):
        await orig_sleep(min(t, 0.05))
    to.asyncio.sleep = fast_sleep
    asyncio.run(main())
