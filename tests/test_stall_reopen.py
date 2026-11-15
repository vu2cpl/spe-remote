"""Stalled-read recovery in SerialHandler (runnable directly or via pytest).

    python3 tests/test_stall_reopen.py      # needs pyserial + pyyaml

Replays the 2026-11-14 fault: the FTDI read URB stopped (kernel
"urb stopped: -32"), the fd stayed open, reads came back empty and writes
still went out, so the amp read "down" until a manual restart. Checks that
the watchdog reopens the port, and above all that the reopen keeps DTR/RTS
where they were: DTR is the amp's remote-power line. A fake port records
the order of HUPCL / close / open events; one case also runs _set_hupcl
against real termios on a pty.
"""
import asyncio
import os
import sys
import termios
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import serial  # noqa: E402

import spe.serial_handler as sh_mod  # noqa: E402

STALL = 0.3
sh_mod._WATCHDOG_TICK = 0.05
sh_mod._STALL_REOPEN_MAX = 1.2

EVENTS = []          # ("hupcl", n, on) / ("close", n) / ("open", n, dtr, rts)
_real_set_hupcl = sh_mod._set_hupcl


def fake_set_hupcl(port, on):
    if getattr(port, "hupcl_fails", False) and not on:
        raise termios.error(25, "Inappropriate ioctl for device")
    EVENTS.append(("hupcl", port.n, on))


class FakeAmp:
    def __init__(self):
        self.powered = True
        self.ports = []
        self.lock = threading.Lock()
        self.hupcl_fails = False

    def stall_open_ports(self):
        with self.lock:
            for p in self.ports:
                if p.is_open:
                    p.stalled = True


class FakePort:
    amp = None

    def __init__(self, port=None, baudrate=None, timeout=0.05, write_timeout=None):
        self.timeout = timeout
        self.port = None
        self.dtr = True         # pyserial defaults
        self.rts = True
        self.is_open = False
        self.stalled = False
        self.out_waiting = 0
        self.fd = -1
        self._rx = bytearray()
        self._cv = threading.Condition()
        self.hupcl_fails = FakePort.amp.hupcl_fails
        with FakePort.amp.lock:
            self.n = len(FakePort.amp.ports)
            FakePort.amp.ports.append(self)
        if port is not None:
            self.port = port
            self.open()

    def open(self):
        self.is_open = True
        EVENTS.append(("open", self.n, self.dtr, self.rts))

    def write(self, data):
        if not self.is_open:
            raise serial.SerialException("closed")
        if FakePort.amp.powered and not self.stalled:
            with self._cv:
                self._rx.extend(b"\x55\x55")    # any bytes prove the amp is alive
                self._cv.notify()

    def flush(self):
        pass

    def read(self, n):
        with self._cv:
            if not self._rx:
                self._cv.wait(self.timeout)
            if not self.is_open:
                raise TypeError("'NoneType' object cannot be interpreted as an integer")
            if self.stalled:
                self._rx.clear()
                return b""
            out = bytes(self._rx[:n])
            del self._rx[:n]
            return out

    def cancel_read(self):
        with self._cv:
            self._cv.notify()

    def close(self):
        if self.is_open:
            EVENTS.append(("close", self.n))
        self.is_open = False


def make_handler(stall_reopen=STALL):
    serial_cfg = SimpleNamespace(port="/dev/fake", baudrate=115200, debug_raw_log="")
    polling = SimpleNamespace(tx_interval=0.05, idle_interval=0.05,
                              stall_reopen=stall_reopen)
    return sh_mod.SerialHandler(serial_cfg, polling, lambda st: None)


async def wait_for(cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


def alive(h):
    return h._last_byte_at and time.monotonic() - h._last_byte_at < 0.2


async def run_handler(h):
    task = asyncio.ensure_future(h.start())
    return task


async def finish(h, task):
    await h.stop()
    task.cancel()
    try:
        await task
    except BaseException:
        pass


async def scenario_stall_recovers_keeping_lines():
    amp = FakeAmp()
    FakePort.amp = amp
    EVENTS.clear()
    h = make_handler()
    task = await run_handler(h)
    assert await wait_for(lambda: alive(h), 2)
    # As after a power-ON sequence: DTR held high, RTS low.
    amp.ports[0].dtr, amp.ports[0].rts = True, False

    amp.stall_open_ports()
    assert await wait_for(lambda: len(amp.ports) == 2 and alive(h), 3), \
        f"no recovery: reopens={h.stall_reopens} events={EVENTS}"
    assert h.stall_reopens == 1
    # HUPCL cleared on the old port BEFORE it closed, so DTR never dropped;
    # new port opened with DTR=1 RTS=0 already applied; HUPCL back on after.
    i_clear = EVENTS.index(("hupcl", 0, False))
    i_close = EVENTS.index(("close", 0))
    i_open = EVENTS.index(("open", 1, True, False))
    i_set = EVENTS.index(("hupcl", 1, True))
    assert i_clear < i_close < i_open < i_set, EVENTS
    # The old reader thread must not tear down the new port.
    await asyncio.sleep(0.5)
    assert amp.ports[1].is_open and h._port is amp.ports[1]
    assert h.stall_reopens == 1      # healthy again: no more reopens
    await finish(h, task)


async def scenario_silent_backs_off_and_power_holds():
    amp = FakeAmp()
    amp.powered = False              # amp switched off: silence, not a fault
    FakePort.amp = amp
    EVENTS.clear()
    h = make_handler()
    task = await run_handler(h)
    await asyncio.sleep(3.0)
    assert 2 <= h.stall_reopens <= 5, h.stall_reopens   # ~10 without backoff
    assert sum(p.is_open for p in amp.ports) == 1
    # Every reopen kept the default lines (DTR=1 RTS=1): no DTR edge.
    assert all(e[2:] == (True, True) for e in EVENTS if e[0] == "open"), EVENTS

    # Power ON click: backoff reset, but no reopen during the hold-off.
    h.note_power_action()
    n = h.stall_reopens
    amp.powered = True
    await asyncio.sleep(STALL * 0.8)
    assert h.stall_reopens == n
    await finish(h, task)


async def scenario_hupcl_failure_skips_reopen():
    amp = FakeAmp()
    amp.powered = False
    amp.hupcl_fails = True
    FakePort.amp = amp
    EVENTS.clear()
    h = make_handler()
    task = await run_handler(h)
    await asyncio.sleep(1.0)
    assert h.stall_reopens == 0 and h._stall_after == 0
    assert len(amp.ports) == 1 and amp.ports[0].is_open, "port closed despite DTR risk"
    await finish(h, task)


def scenario_real_termios_hupcl():
    """_set_hupcl against a real tty (pty): clears and restores the bit."""
    master, slave = os.openpty()
    try:
        port = SimpleNamespace(fd=slave)
        _real_set_hupcl(port, False)
        assert not termios.tcgetattr(slave)[2] & termios.HUPCL
        _real_set_hupcl(port, True)
        assert termios.tcgetattr(slave)[2] & termios.HUPCL
    finally:
        os.close(master)
        os.close(slave)


def main():
    real_serial = sh_mod.serial.Serial
    sh_mod.serial.Serial = FakePort
    sh_mod._set_hupcl = fake_set_hupcl
    try:
        for sc in (scenario_stall_recovers_keeping_lines,
                   scenario_silent_backs_off_and_power_holds,
                   scenario_hupcl_failure_skips_reopen):
            asyncio.run(sc())
            print(f"  ok  {sc.__name__}")
    finally:
        sh_mod.serial.Serial = real_serial
        sh_mod._set_hupcl = _real_set_hupcl
    scenario_real_termios_hupcl()
    print("  ok  scenario_real_termios_hupcl")
    print("All stall-reopen tests passed.")


def test_stall_reopen():
    main()


if __name__ == "__main__":
    main()
