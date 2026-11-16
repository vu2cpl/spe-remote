"""What a new WebSocket client is told at connect.

The server caches the amp's last state. A client that connects while the
amp is silent (powered off, FTDI port still open) must not be handed that
cached state: clients take a state frame as "the amp is on". On
2026-11-17 a dashboard showed ON for hours with the amp off, so its power
toggle sent power_off instead of power_on.

At connect the server sends:
  - amp replying -> the current state, then the presence heartbeat (up)
  - amp silent   -> only the presence heartbeat (down)

Real connections against a real Tornado server on a loopback port, so this
needs tornado (`venv/bin/python tests/test_ws_connect_snapshot.py` on the
Pi, or any env with `pip install tornado pyserial`). Without them it
prints SKIP and exits 0. No amplifier, no serial port.
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import serial  # noqa: F401  (spe.serial_handler imports it)
    import tornado.httpserver
    import tornado.testing
    import tornado.web
    import tornado.websocket
except ImportError as e:
    print(f"[SKIP] {e.name} not installed")
    sys.exit(0)

from spe.protocol import AmplifierState
from spe.serial_handler import SerialHandler
from spe.websocket_handler import AmplifierWebSocket

FAILURES = []
THRESHOLD = 3.0
INF = float("inf")


def check(label, cond, detail=""):
    if cond:
        print(f"[PASS] {label}")
    else:
        FAILURES.append(label)
        print(f"[FAIL] {label} {detail}")


class FakeSerial:
    """Only what open() and the heartbeat read. amp_alive is the real
    method, so the test follows the server's own definition of alive."""

    def __init__(self):
        self.state = AmplifierState()
        self.last_state_age = INF
        self.last_rcu_age = INF

    amp_alive = SerialHandler.amp_alive


async def messages_at_connect(port, settle=0.3):
    """Everything the server sends in the first `settle` seconds."""
    conn = await tornado.websocket.websocket_connect(f"ws://127.0.0.1:{port}/ws")
    got = []
    try:
        while True:
            msg = await asyncio.wait_for(conn.read_message(), settle)
            if msg is None:
                break
            got.append(json.loads(msg))
    except asyncio.TimeoutError:
        pass
    conn.close()
    return got


def kinds(msgs):
    return ["heartbeat:" + m["serial"] if m.get("heartbeat")
            else "state:" + m.get("op_status", "?") for m in msgs]


async def main():
    logging.getLogger("tornado.access").setLevel(logging.ERROR)
    fake = FakeSerial()
    AmplifierWebSocket.configure(fake, amp_alive_threshold=THRESHOLD)
    sock, port = tornado.testing.bind_unused_port()
    server = tornado.httpserver.HTTPServer(
        tornado.web.Application([(r"/ws", AmplifierWebSocket)]))
    server.add_sockets([sock])

    # 1. Fresh server, no frame seen yet.
    got = kinds(await messages_at_connect(port))
    check("no frame ever seen: heartbeat down only", got == ["heartbeat:down"], got)

    # 2. The 2026-11-17 case: amp went off in Stby hours ago.
    fake.state.op_status = "Stby"
    fake.last_state_age = 6 * 3600.0
    fake.last_rcu_age = 6 * 3600.0
    got = kinds(await messages_at_connect(port))
    check("amp silent with a cached Stby: cached state is not sent",
          got == ["heartbeat:down"], got)

    # 3. Amp replying: state first, then heartbeat up.
    fake.state.op_status = "Oper"
    fake.last_state_age = 0.2
    fake.last_rcu_age = 0.1
    got = kinds(await messages_at_connect(port))
    check("amp replying: state, then heartbeat up",
          got == ["state:Oper", "heartbeat:up"], got)

    # 4. STANDBY: CSV slower than the threshold, RCU frames keep coming.
    fake.state.op_status = "Stby"
    fake.last_state_age = THRESHOLD + 5
    fake.last_rcu_age = 0.4
    got = kinds(await messages_at_connect(port))
    check("standby (RCU frames only): state is still sent",
          got == ["state:Stby", "heartbeat:up"], got)

    # 5. Just over the threshold on both: treated as silent.
    fake.last_state_age = THRESHOLD + 0.1
    fake.last_rcu_age = THRESHOLD + 0.1
    got = kinds(await messages_at_connect(port))
    check("both ages over the threshold: heartbeat down only",
          got == ["heartbeat:down"], got)

    # 6. The connect heartbeat has the same fields as the periodic one.
    msgs = await messages_at_connect(port)
    hb = msgs[-1] if msgs else {}
    check("connect heartbeat carries heartbeat/serial/ts/clients",
          set(hb) == {"heartbeat", "serial", "ts", "clients"} and hb.get("clients") == 1, hb)

    server.stop()
    AmplifierWebSocket.configure(None)
    AmplifierWebSocket.clients.clear()


asyncio.run(main())
if FAILURES:
    print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("\nAll passed")
