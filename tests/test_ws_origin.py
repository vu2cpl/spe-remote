"""Origin policy of the amplifier WebSocket (cross-site WebSocket hijacking).

A web page on another site, open in a browser on the operator's LAN, must
not be able to open /ws and send power_on / oper / tune. The web UI served
by this server (same origin), a TLS proxy in front of it, and Origin-less
native clients (MacExpert, Node-RED) must still connect.

Real handshakes against a real Tornado server on a loopback port, so this
needs tornado (`venv/bin/python tests/test_ws_origin.py` on the Pi, or any
env with `pip install tornado`). Without tornado it prints SKIP and exits 0.
No amplifier, no serial port.
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import tornado.httpclient
    import tornado.httpserver
    import tornado.testing
    import tornado.web
    import tornado.websocket
except ImportError:
    print("[SKIP] tornado not installed")
    sys.exit(0)

from spe.websocket_handler import AmplifierWebSocket

FAILURES = []


def check(label, cond, detail=""):
    if cond:
        print(f"[PASS] {label}")
    else:
        FAILURES.append(label)
        print(f"[FAIL] {label} {detail}")


async def handshake(port, origin):
    """Return the HTTP status of the upgrade: 101 on success."""
    headers = {"Origin": origin} if origin else {}
    req = tornado.httpclient.HTTPRequest(f"ws://127.0.0.1:{port}/ws", headers=headers)
    try:
        conn = await tornado.websocket.websocket_connect(req)
    except tornado.httpclient.HTTPClientError as e:
        return e.code
    conn.close()
    return 101


async def main():
    logging.getLogger("tornado.access").setLevel(logging.ERROR)  # the 403s are expected
    sock, port = tornado.testing.bind_unused_port()
    server = tornado.httpserver.HTTPServer(
        tornado.web.Application([(r"/ws", AmplifierWebSocket)]))
    server.add_sockets([sock])
    here = f"127.0.0.1:{port}"
    cases = [
        ("no Origin (MacExpert, Node-RED) connects", None, 101),
        ("same origin (served web UI) connects", f"http://{here}", 101),
        ("same origin through a TLS proxy connects", f"https://{here}", 101),
        ("another site is refused", "https://evil.example", 403),
        ("same host, other port (:1880) is refused", "http://127.0.0.1:1880", 403),
    ]
    for label, origin, want in cases:
        got = await handshake(port, origin)
        check(label, got == want, f"(Origin {origin!r}: got {got}, want {want})")
    server.stop()


asyncio.run(main())
print("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED")
sys.exit(1 if FAILURES else 0)
