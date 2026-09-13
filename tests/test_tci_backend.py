"""Fake-socket tests for the TCI (ExpertSDR3 / SunSDR) backend.

Covers the wire commands, the Hz↔MHz boundary between TCI and the
RadioConnection interface, and what happens when the radio drops the
socket. No hardware and no real WebSocket — FakeWS stands in for
tornado's client, so these run anywhere.

The units cases are the counterpart to the Flex "identical commands"
check: TCI talks Hz, the orchestrator's band check (band_for_freq) is
MHz-only, and the conversion lives only in spe/tci.py.
"""
import asyncio
import sys
import types

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILURES = []


def check(label, cond, detail=""):
    if cond:
        print(f"[PASS] {label}")
    else:
        FAILURES.append(label)
        print(f"[FAIL] {label} {detail}")


class FakeWS:
    """Stands in for tornado's WebSocketClientConnection."""

    def __init__(self):
        self.sent = []
        self.closed = False
        self._inbox = asyncio.Queue()

    async def write_message(self, message):
        if self.closed:
            raise ConnectionError("socket closed")
        self.sent.append(message)

    async def read_message(self):
        return await self._inbox.get()

    def close(self):
        self.closed = True

    def push(self, message):
        """Queue a frame from the 'radio' (None = socket closed)."""
        self._inbox.put_nowait(message)


LAST_WS = []


async def _fake_connect(url, connect_timeout=None):
    ws = FakeWS()
    LAST_WS.append(ws)
    return ws


# spe.tci imports these at module load; no real socket is ever opened.
_fake_ws_mod = types.ModuleType("tornado.websocket")
_fake_ws_mod.websocket_connect = _fake_connect
_fake_ws_mod.WebSocketClientConnection = FakeWS
_fake_tornado = types.ModuleType("tornado")
_fake_tornado.websocket = _fake_ws_mod
sys.modules["tornado"] = _fake_tornado
sys.modules["tornado.websocket"] = _fake_ws_mod

from spe.tci import TciConnection             # noqa: E402
from spe.spe_band_table import band_for_freq  # noqa: E402


async def connected(**kw):
    """A TciConnection whose socket is up and whose startup dump has
    landed (`ready;`), so connect() doesn't sit out its timeout."""
    conn = TciConnection("192.0.2.10", **kw)
    task = asyncio.ensure_future(conn.connect())
    await asyncio.sleep(0)               # let connect() open the socket
    ws = LAST_WS[-1]
    ws.push("ready;")
    await task
    return conn, ws


async def t1_wire_commands():
    """The three commands the tune cycle sends, exactly as documented."""
    conn, ws = await connected()
    ws.sent.clear()
    await conn.set_frequency(0, 14.025)
    await conn.set_mode(0, "CW")
    await conn.tune_carrier(True)
    await conn.tune_carrier(False)
    check("t1 tci wire commands",
          ws.sent == ["vfo:0,0,14025000;", "modulation:0,CW;",
                      "tune:0,true;", "tune:0,false;"], str(ws.sent))
    await conn.close()


async def t2_connect_writes_nothing():
    """connect() must not write to the radio: TCI's two-arg `vfo:0,0;`
    request form is ambiguous enough that a firmware revision could read
    it as 'set VFO to 0 Hz' and trash the operator's dial."""
    conn, ws = await connected()
    check("t2 connect sends no commands", ws.sent == [], str(ws.sent))
    await conn.close()
    # close() must reach the real socket even though the read loop's
    # teardown has already cleared the connection's reference to it.
    check("t2 close() closes the socket", ws.closed)
    check("t2 is_connected False after close", not conn.is_connected)


async def t3_snapshot_is_mhz():
    """The event cache holds Hz; snapshot() hands the interface MHz, so
    band_for_freq() (MHz-only) can resolve the band."""
    conn, ws = await connected()
    ws.push("vfo:0,0,14025000;modulation:0,CW;")
    await asyncio.sleep(0.01)
    snap = conn.snapshot(0)
    check("t3 snapshot freq in MHz",
          snap and abs(snap["freq"] - 14.025) < 1e-9, str(snap))
    check("t3 band_for_freq resolves 20m",
          band_for_freq(snap["freq"]) == "20m",
          str(band_for_freq(snap["freq"])))
    check("t3 snapshot carries the mode", snap["mode"] == "CW", str(snap))
    await conn.close()


async def t4_restore_round_trips():
    """restore() puts back exactly the Hz the radio reported."""
    conn, ws = await connected()
    ws.push("vfo:0,0,7025000;modulation:0,DIGU;")
    await asyncio.sleep(0.01)
    snap = conn.snapshot(0)
    ws.sent.clear()
    await conn.restore(snap)
    check("t4 restore round-trips freq+mode",
          ws.sent == ["vfo:0,0,7025000;", "modulation:0,DIGU;"], str(ws.sent))
    await conn.close()


async def t5_fractional_hz_survives():
    """A fractional Hz from a firmware quirk must not raise — int() did."""
    conn, ws = await connected()
    ws.push("vfo:0,0,14025000.4;")
    await asyncio.sleep(0.01)
    snap = conn.snapshot(0)
    ws.sent.clear()
    await conn.restore(snap)
    check("t5 fractional Hz restores (rounded)",
          ws.sent == ["vfo:0,0,14025000;"], str(ws.sent))
    await conn.close()


async def t6_unparsable_freq_is_unknown():
    """Garbage freq ⇒ 'radio band unknown', not a crash."""
    conn, ws = await connected()
    ws.push("vfo:0,0,notanumber;")
    await asyncio.sleep(0.01)
    snap = conn.snapshot(0)
    check("t6 unparsable freq ⇒ None", snap is None or snap["freq"] is None,
          str(snap))
    await conn.close()


async def t7_socket_close_clears_ws():
    """When the radio drops the socket, is_connected must go False so
    the next tune reconnects instead of writing into a dead socket."""
    conn, ws = await connected()
    check("t7 connected before", conn.is_connected)
    ws.push(None)                        # radio closed the socket
    await asyncio.sleep(0.01)
    check("t7 is_connected False after close", not conn.is_connected)
    await conn.close()


async def t8_restore_failure_propagates():
    """A failed restore must raise so the orchestrator reports FAIL
    instead of emitting VFO_RESTORED for a radio still parked on the
    last swept sub-band."""
    conn, ws = await connected()
    ws.push("vfo:0,0,14025000;")
    await asyncio.sleep(0.01)
    snap = conn.snapshot(0)
    ws.push(None)                        # socket dies mid-sweep
    await asyncio.sleep(0.01)
    raised = False
    try:
        await conn.restore(snap)
    except Exception:
        raised = True
    check("t8 restore raises on a dead socket", raised)
    await conn.close()


async def main():
    for t in (t1_wire_commands, t2_connect_writes_nothing,
              t3_snapshot_is_mhz, t4_restore_round_trips,
              t5_fractional_hz_survives, t6_unparsable_freq_is_unknown,
              t7_socket_close_clears_ws, t8_restore_failure_propagates):
        await t()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
