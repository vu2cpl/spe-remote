"""Fake-socket tests for the Flex (SmartSDR) backend.

Two jobs. First, pin the property the multi-radio refactor rests on:
driving FlexConnection through the *generic* RadioConnection methods
produces byte-for-byte the same SmartSDR commands the slice-oriented
methods always did — the refactor's safety net, previously checked only
locally. Second, cover restore's failure path, which must re-raise so a
slice left on the last swept sub-band can't be reported as VFO_RESTORED
(the same shape as tests/test_tci_backend.py's t8).

No socket is opened: FlexConnection is built but never connected, and
`send` is either recorded or left to raise ConnectionError as it does on
a dead link.
"""
import asyncio
import sys

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spe.flex import FlexConnection             # noqa: E402
from spe.spe_band_table import band_for_freq    # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    if cond:
        print(f"[PASS] {label}")
    else:
        FAILURES.append(label)
        print(f"[FAIL] {label} {detail}")


def recording(conn):
    """Swap in a `send` that records instead of writing to a socket."""
    sent = []

    async def fake_send(command):
        sent.append(command)
        return ""

    conn.send = fake_send
    return sent


async def t1_generic_methods_are_identical():
    """The generic interface must emit exactly the commands the
    slice-oriented methods emit — the Flex wire behaviour is unchanged
    by the abstraction."""
    conn = FlexConnection("192.0.2.20")
    sent = recording(conn)
    await conn.set_frequency(0, 14.025)
    await conn.set_mode(0, "CW")
    await conn.set_tune_power(10)
    await conn.tune_carrier(True)
    await conn.tune_carrier(False)
    generic = list(sent)

    sent.clear()
    await conn.set_slice_freq(0, 14.025)
    await conn.set_slice_mode(0, "CWU")
    await conn.set_tune_power(10)
    await conn.tune_carrier(True)
    await conn.tune_carrier(False)

    check("t1 generic == slice-oriented commands", generic == sent,
          f"{generic} != {sent}")
    check("t1 the documented wire commands",
          generic == ["slice t 0 14.025000", "slice s 0 mode=CWU",
                      "transmit set tunepower=10",
                      "transmit tune on", "transmit tune off"], str(generic))


async def t2_mode_mapping():
    """CW→CWU for the tune carrier; anything else (a restored USB/DIGU)
    passes through verbatim."""
    conn = FlexConnection("192.0.2.20")
    sent = recording(conn)
    await conn.set_mode(0, "CW")
    await conn.set_mode(0, "cw")
    await conn.set_mode(0, "USB")
    await conn.set_mode(0, "DIGU")
    check("t2 CW maps to CWU (either case)",
          sent[:2] == ["slice s 0 mode=CWU", "slice s 0 mode=CWU"], str(sent))
    check("t2 other modes pass through",
          sent[2:] == ["slice s 0 mode=USB", "slice s 0 mode=DIGU"], str(sent))


async def t3_snapshot_is_mhz():
    """The slice cache already holds MHz; snapshot passes it through, so
    band_for_freq() resolves the band the same way it does for TCI."""
    conn = FlexConnection("192.0.2.20")
    check("t3 empty cache ⇒ None", conn.snapshot(0) is None)
    conn.slice_state[0] = {"RF_frequency": "14.025000", "mode": "DIGU"}
    snap = conn.snapshot(0)
    check("t3 snapshot carries freq+mode",
          snap == {"channel": 0, "freq": "14.025000", "mode": "DIGU"},
          str(snap))
    check("t3 band_for_freq resolves 20m",
          band_for_freq(float(snap["freq"])) == "20m")


async def t4_restore_round_trips():
    conn = FlexConnection("192.0.2.20")
    conn.slice_state[0] = {"RF_frequency": "7.025000", "mode": "DIGU"}
    snap = conn.snapshot(0)
    sent = recording(conn)
    await conn.restore(snap)
    check("t4 restore round-trips freq+mode",
          sent == ["slice t 0 7.025000", "slice s 0 mode=DIGU"], str(sent))


async def t5_restore_failure_propagates():
    """A failed restore must raise so the orchestrator reports FAIL
    instead of emitting VFO_RESTORED for a slice still parked on the
    last swept sub-band. Never connected ⇒ send() raises
    ConnectionError, the same as a link that dropped mid-sweep."""
    conn = FlexConnection("192.0.2.20")
    conn.slice_state[0] = {"RF_frequency": "14.025000", "mode": "USB"}
    snap = conn.snapshot(0)
    raised = False
    try:
        await conn.restore(snap)
    except Exception:
        raised = True
    check("t5 restore raises on a dead socket", raised)


async def t6_restore_none_is_noop():
    conn = FlexConnection("192.0.2.20")
    sent = recording(conn)
    await conn.restore(None)
    check("t6 restore(None) writes nothing", sent == [], str(sent))


async def main():
    for t in (t1_generic_methods_are_identical, t2_mode_mapping,
              t3_snapshot_is_mhz, t4_restore_round_trips,
              t5_restore_failure_propagates, t6_restore_none_is_noop):
        await t()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
