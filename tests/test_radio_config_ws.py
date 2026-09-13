"""Fake-driven tests for the `set_radio_config` WebSocket handler.

Covers what a client can talk the server into writing to `config.yaml`:
field validation, the live swap being handed to the controller in one
locked step, and a failed persist being reported instead of swallowed
(the client used to be told the change stuck, then lose it on the next
restart). No tornado, no pyyaml, no sockets.
"""
import asyncio
import json
import sys
import types
from dataclasses import dataclass, field

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILURES = []


def check(label, cond, detail=""):
    if cond:
        print(f"[PASS] {label}")
    else:
        FAILURES.append(label)
        print(f"[FAIL] {label} {detail}")


# --- stubs: tornado (not installed Mac-side) and the config module ----
class _WSClosed(Exception):
    pass


class _WSHandler:
    def __init__(self, *a, **kw):
        pass


_t_ws = types.ModuleType("tornado.websocket")
_t_ws.WebSocketHandler = _WSHandler
_t_ws.WebSocketClosedError = _WSClosed
_tornado = types.ModuleType("tornado")
_tornado.websocket = _t_ws
sys.modules["tornado"] = _tornado
sys.modules["tornado.websocket"] = _t_ws


@dataclass
class RadioConfig:
    kind: str = "none"


@dataclass
class FlexConfig:
    enabled: bool = False
    host: str = ""
    port: int = 4992
    slice_rx: int = 0
    tune_power_watts: int = 10


@dataclass
class TciConfig:
    host: str = "127.0.0.1"
    port: int = 50001
    trx: int = 0
    mode: str = "CW"
    tune_drive: int = 0


@dataclass
class AppConfig:
    radio: RadioConfig = field(default_factory=RadioConfig)
    flex: FlexConfig = field(default_factory=FlexConfig)
    tci: TciConfig = field(default_factory=TciConfig)


PERSIST_CALLS = []
PERSIST_OK = True


def persist_values(changes, path="config.yaml"):
    PERSIST_CALLS.append(dict(changes))
    return PERSIST_OK


_cfg_mod = types.ModuleType("spe.config")
_cfg_mod.persist_values = persist_values
_cfg_mod.RadioConfig = RadioConfig
_cfg_mod.FlexConfig = FlexConfig
_cfg_mod.TciConfig = TciConfig
sys.modules["spe.config"] = _cfg_mod

from spe.websocket_handler import AmplifierWebSocket  # noqa: E402


class FakeClient:
    def __init__(self):
        self.messages = []

    def write_message(self, msg):
        self.messages.append(json.loads(msg))


class FakeController:
    """Records what reconfigure() was handed, and whether the caller
    went through it rather than poking config objects by hand."""

    def __init__(self):
        self.reconfigured = []

    async def reconfigure(self, radio, flex, tci):
        self.reconfigured.append((radio, flex, tci))

    async def disconnect(self):
        raise AssertionError("handler must use reconfigure(), not disconnect()")


def setup(persist_ok=True):
    global PERSIST_OK
    PERSIST_OK = persist_ok
    PERSIST_CALLS.clear()
    cfg = AppConfig()
    ctrl = FakeController()
    client = FakeClient()
    AmplifierWebSocket.clients = {client}
    AmplifierWebSocket.configure(None, radio_controller=ctrl, app_config=cfg,
                                 config_path="config.yaml")
    handler = AmplifierWebSocket.__new__(AmplifierWebSocket)
    return handler, cfg, ctrl, client


def events(client, phase=None):
    out = [m for m in client.messages if "tune_event" in m]
    return [m for m in out if phase is None or m["tune_event"] == phase]


async def t1_applies_and_persists():
    h, cfg, ctrl, client = setup()
    await h._handle_set_radio_config(json.dumps(
        {"kind": "tci", "tci": {"host": "192.168.1.10", "trx": 1}}))
    check("t1 controller reconfigured once", len(ctrl.reconfigured) == 1)
    radio, flex, tci = ctrl.reconfigured[0]
    check("t1 new kind handed over", radio.kind == "tci", str(radio))
    check("t1 new tci settings handed over",
          (tci.host, tci.trx, tci.port) == ("192.168.1.10", 1, 50001), str(tci))
    check("t1 flex.enabled follows the selector", flex.enabled is False)
    check("t1 app config updated",
          (cfg.radio.kind, cfg.tci.host) == ("tci", "192.168.1.10"))
    check("t1 persisted the fields sent",
          PERSIST_CALLS == [{"radio.kind": "tci", "flex.enabled": False,
                             "tci.host": "192.168.1.10", "tci.trx": 1}],
          str(PERSIST_CALLS))
    check("t1 RADIO_CONFIG_UPDATED broadcast",
          len(events(client, "RADIO_CONFIG_UPDATED")) == 1)
    check("t1 no RADIO_ERROR", not events(client, "RADIO_ERROR"))
    snap = [m for m in client.messages if m.get("config_event") == "radio"]
    check("t1 config snapshot says persisted",
          len(snap) == 1 and snap[0]["persisted"] is True, str(snap))


async def t2_persist_failure_is_reported():
    h, cfg, ctrl, client = setup(persist_ok=False)
    await h._handle_set_radio_config(json.dumps({"kind": "flex"}))
    check("t2 still applied live", cfg.radio.kind == "flex")
    check("t2 RADIO_ERROR raised", len(events(client, "RADIO_ERROR")) == 1,
          str(events(client)))
    updated = events(client, "RADIO_CONFIG_UPDATED")
    check("t2 update message says not saved",
          updated and "not saved" in updated[0]["tune_message"], str(updated))
    snap = [m for m in client.messages if m.get("config_event") == "radio"]
    check("t2 config snapshot says persisted:false",
          snap and snap[0]["persisted"] is False, str(snap))


async def t3_out_of_range_refused():
    for payload, why in (
        ({"kind": "flex", "flex": {"slice_rx": 99}}, "slice_rx 99"),
        ({"kind": "flex", "flex": {"port": -1}}, "negative port"),
        ({"kind": "tci", "tci": {"tune_drive": 500}}, "tune_drive 500"),
        ({"kind": "tci", "tci": {"trx": 4}}, "trx 4"),
        ({"kind": "flex", "flex": {"port": "not-a-port"}}, "non-numeric port"),
        ({"kind": "tci", "tci": {"mode": "C W;"}}, "mode with punctuation"),
    ):
        h, cfg, ctrl, client = setup()
        await h._handle_set_radio_config(json.dumps(payload))
        check(f"t3 {why} refused", len(events(client, "RADIO_ERROR")) == 1,
              str(events(client)))
        check(f"t3 {why} nothing applied", not ctrl.reconfigured)
        check(f"t3 {why} nothing persisted", not PERSIST_CALLS)
        check(f"t3 {why} config untouched", cfg.radio.kind == "none")


async def t4_valid_edges_accepted():
    h, cfg, ctrl, client = setup()
    await h._handle_set_radio_config(json.dumps(
        {"kind": "flex", "flex": {"host": "", "port": 65535, "slice_rx": 7,
                                  "tune_power_watts": 1}}))
    check("t4 edge values accepted", len(ctrl.reconfigured) == 1,
          str(events(client)))
    check("t4 empty host kept (auto-discover)",
          ctrl.reconfigured[0][1].host == "")


async def t5_refused_mid_tune():
    h, cfg, ctrl, client = setup()

    class Running:
        is_running = True
    AmplifierWebSocket._tune_orchestrator = Running()
    await h._handle_set_radio_config(json.dumps({"kind": "tci"}))
    AmplifierWebSocket._tune_orchestrator = None
    check("t5 refused while tuning", not ctrl.reconfigured)
    check("t5 RADIO_ERROR says why",
          events(client, "RADIO_ERROR") and
          "tune is running" in events(client, "RADIO_ERROR")[0]["tune_message"])


async def main():
    for t in (t1_applies_and_persists, t2_persist_failure_is_reported,
              t3_out_of_range_refused, t4_valid_edges_accepted,
              t5_refused_mid_tune):
        await t()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
