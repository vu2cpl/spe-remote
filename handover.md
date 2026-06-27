# spe-remote — Handover

**Repo:** `~/projects/spe-remote` (canonical; the copy under `~/Documents/Claude/code/spe-remote` is stale, do not touch)
**Branch:** `main`, clean, in sync with `origin/main`
**Latest release:** `v3.0.0` — Flex orchestration + band sweep (2026-06-20)
**Last commit:** SPE power meter — server-side AVG (EMA) + PEAK (peak-hold) `p_out` fields
**Date:** 2026-06-27

## What this project is

Python 3 remote-control server for **SPE Expert** HF amplifiers (1.3K-FA, 1.5K-FA, 2K-FA). Runs on a Raspberry Pi, opens the USB-serial port to the amp, and serves a single WebSocket on `:8888` that fans out to:

- the bundled browser dashboard at `http://pi:8888`
- Node-RED on the Pi
- the MacExpert companion app

The Python server is the **only** process that ever opens `/dev/ttyUSB0`. Every other client speaks WebSocket. This is the whole reason the project exists — Node-RED used to hold the serial port directly and locked everyone else out.

## Layout

```
server.py              main async server (serial reader thread + ws broadcast)
power_spe_on.py        DTR-line power-on helper
config.yaml            serial port, baud, poll intervals, temp unit, log level
spe/                   protocol module (parser, command builder, RCU framing)
web/                   browser dashboard (static HTML/JS/CSS, canvas gauges)
systemd/               unit file for spe-remote.service
install-service.sh     installs + enables the systemd unit
uninstall-service.sh   reverse of above
run.sh                 dev launcher
setup.sh               first-time venv + deps install
docs/                  protocol notes, Node-RED sample flow
```

## Recent work (newest first)

1. **SPE power meter: server-side AVG (EMA) + PEAK (peak-hold) (2026-06-27)** — `AmplifierState` gains two `p_out`-derived float fields: `p_out_avg` (EMA, α=0.15, ~1 s settle at the 25 Hz TX poll) and `p_out_peak` (peak-hold — pins the highest sample for 2.5 s then decays toward the live reading at a constant 600 W/s). Both computed in `SerialHandler._consume_csv_frame` and reset across an op/tx transition so a fresh TX isn't dragged by the prior idle reading; raw `p_out` is untouched, so existing consumers are unaffected. Lets clients offer RAW / AVG / PEAK meter modes without each re-deriving the ballistics — the `vu2cpl-shack` dashboards (D1 `/ui` + Vue `/shack`) consume them through a pill toggle. **`p_out_peak` is a *sampled* peak, not true envelope PEP** (25 Hz → ~40 ms blind spots); the field doc-comment + README say so. Origin: a contributed feature package (adersh fork) whose peak-decay math compounded `decay_s` every frame — an accelerating, sample-rate-dependent fall (1500→0 in ~0.46 s) instead of the documented constant 600 W/s. The version landed here uses a per-frame `dt`-scaled decrement (verified 2.50 s for 1500→0, frame-rate independent). If that package is ever re-imported, re-apply this fix.
2. **v3.0.0 — first tagged release (2026-06-20)** — cut the very first git tag + GitHub release on the project. Headline feature surface for the version: FlexRadio 6000-series orchestration (TCP client, auto-discovery, tune orchestrator, 154-sub-band sweep); the interactive configurator + service-update flow (PR #1, VU3ESV); plus all the resilience work that landed alongside (presence-heartbeat, RCU-counts-toward-heartbeat, strict CSV parse, TUNE LED mirror, live °C/°F toggle, auto-detect model). The informal "2.1 (RCU + threaded reader)" string in `docs/generate_guide.py` was bumped to `3.0.0 (Flex orchestration + band sweep)`; semver-major from 2.1 reflects the new Flex capability surface. Going forward: cut a tag whenever a coherent feature line lands, write release notes against the commit range since the last tag. **CDP follow-up:** `docs/SPE_Remote_Control_User_Guide.pdf` was regenerated from the updated `generate_guide.py` shortly after tagging (the PDF had been baked at "Version 2.1" since 2026-04-21), and the `v3.0.0` tag was force-moved forward by one commit to include the new PDF in the GitHub source tarball. Lesson for the next release: regenerate the PDF as part of the release commit, not after.
3. **Interactive configurator + service-update flow (2026-06-20, PR #1 by VU3ESV/LB9KJ)** — first-time install on a Pi used to mean hand-editing `config.yaml` to find the right `/dev/serial/by-id/...` path, and re-installs could clobber the saved port and Flex IP. New `configtool.py` does comment-preserving `get`/`preview`/`write` against `config.yaml`, editing only the host-specific keys (`serial.port`, `flex.*`) via section-aware line substitutions so all the explanatory comments survive (a PyYAML round-trip would drop them). Synthesises a `flex:` block if an older config lacks one; PyYAML is imported lazily so `preview`/`write` work without it. New `configure.sh` interactive setup lists `/dev/serial/by-id/` aliases for menu pick on a Pi, makes Flex optional, presents current values as defaults so re-runs never lose the port or Flex IP, and shows a diff + confirmation before writing. `setup.sh` now runs `configure.sh` on first install (TTY-attached) and drops a `.configured` marker so it doesn't re-prompt. `install-service.sh --update` / `--reconfigure` re-runs the configurator (diff + retain) as the service user and restarts the running service. Plain installs deliberately never touch `config.yaml` so settings carry across upgrades.
4. **Committed config.yaml synced to Pi production (2026-06-19)** — the in-repo `config.yaml` had drifted: missing the `flex:` block, and still carrying the experimental `tx_interval: 0.1` / `idle_interval: 0.4` polling tweak that was rolled back on the Pi months ago. Updated so a fresh `git pull` on noderedpi4 lands the same shape that's actually running: polling intervals restored to the 0.2 / 1.0 defaults, `temperature_unit: C` (uppercased), and a fully-commented `flex:` block (`enabled: true`, `host: 192.168.1.148`, slice 0, 10 W tune power). The Pi's `serial.debug_raw_log` is deliberately NOT committed — it's the investigation-only knob from item 6 below and shouldn't be the default for fresh clones.
5. **Phase 2b: band sweep against the SPE manual's sub-band table (2026-06-19)** — new `spe/spe_band_table.py` carries the 154 sub-band central frequencies from the SPE 1.5K-FA User Manual rev 3.2 Section 19, keyed by SPE band name (`160m`, `80m`, …, `6m`). `TuneOrchestrator.tune_band(band)` looks up the band's freqs and loops a shared `_run_one_cycle(freq_mhz)` (refactored out of the old tune_single body) over each one. New phase strings `SWEEP_STARTED` / `SWEEP_STEP` / `SWEEP_DONE` augment the per-cycle PHASES so clients can render progress without parsing message text. `tune_stop` works the same — the sweep checks `_stop_requested` between sub-bands so abort lands within one cycle. New WS command: `tune_band:<band>` (case-insensitive band name, tolerant of `20` vs `20m`). Per the manual's instructions the operator picks band + antenna before triggering; spe-remote doesn't touch either — we just sweep the freqs the manual lists. MacExpert UI for the Sweep panel still pending; today the only way to trigger is via `echo tune_band:20m | websocat ws://192.168.1.169:8888/ws`.
6. **Phase 2a of band-sweep work: tune orchestrator + WS commands (2026-06-19)** — new `spe/tune_orchestrator.py` (`TuneOrchestrator`) coordinates the SPE-side TUNE keycode and Flex-side carrier into one ATU tune cycle, driven by the RCU TUNE-LED bit (no blind timing). Flow: preflight (amp in STBY, LED off) → optional `slice t <n> <freq>` → `transmit set tunepower=<W>` → SPE TUNE keycode via existing `send_command("tune")` → wait for `serial.last_tune_active == True` (LED on, max 2 s) → `transmit tune on` → wait for `last_tune_active == False` (ATU done, max 10 s) → `transmit tune off`. Carrier-off lives in a finally block — a crash or `tune_stop` mid-cycle always drops the carrier. New WS commands: `tune_single` (run a cycle on current Flex freq) and `tune_stop` (abort). Phase progress broadcast to all clients as `{"tune_event": <PHASE>, "tune_message": "...", "ts": ...}` JSON; terminal phases are SUCCESS / FAIL / ABORT. The ethernet-interlock dance (`interlock create type=AMP ...`) deliberately skipped — firmware 1.4.0.0 on the test rig rejected the command, and direct `transmit tune on` works without it; re-evaluate when the radio is on newer firmware. The MacExpert TUNE button doesn't go through this yet (still uses the existing `serial.send_command("tune")` for the keycode without Flex coordination); MacExpert UI for `tune_single` lands in Phase 2b alongside the band-sweep loop.
7. **TUNE bit mirrored on Pi (2026-06-19)** — `SerialHandler._emit_rcu_frame` decodes byte 4 bit 6 of every RCU LCD payload (CLEAR = front-panel TUNE LED on; the bit was identified in MacExpert via labelled-diff capture, see `macexpert-spe/tools/find_tune_bit.py`). Exposed as the public property `serial_handler.last_tune_active`, stamped onto every `AmplifierState.tune_active` field broadcast on the state JSON. The Phase-2a tune orchestrator reads it directly off the handler (no JSON roundtrip, sees transitions within ~one RCU tick = 0.5 s); non-MacExpert clients (Node-RED, browser dashboard) consume the JSON field.
8. **Phase 1 of band-sweep work: FlexRadio TCP client (2026-06-19)** — new `spe/flex.py` (`FlexConnection`) talks the SmartSDR TCP/IP API (port 4992, `C<seq>|cmd` / `R<seq>|status|msg` framing, `S<handle>|msg` async events). Minimal command set: `slice s <n> mode=<m>`, `slice t <n> <freq_mhz>`, `transmit set tunepower=<w>`, `transmit tune on|off`, `xmit 0|1`, `sub <topic> all`. New `FlexConfig` (enabled / host / port / slice_rx / tune_power_watts; disabled by default), wired into `AppConfig`. `spe/flex_cli.py` is a safe manual driver — defaults to read-only commands, requires `--allow-tx` to accept anything that could key the radio. **Not yet integrated** with the main poll/broadcast loops; that's Phase 2 (tune-flow orchestration that wires Flex carrier + SPE TUNE keycode + watch byte-4-bit-6 in RCU for ATU-done). The amp-side tune bit was confirmed on the same day — see macexpert-spe `b6c39a1` for the RCUFrame.isTuneActive accessor and tools/find_tune_bit.py for the discovery method.
9. **RCU tick interval 1.5s → 0.5s (2026-06-19)** — `_RCU_TICK_INTERVAL` in `serial_handler.py`. The amp only emits an RCU frame when we toggle RCU_OFF/ON, so this interval is the worst-case latency for the app to discover front-panel events. With MacExpert's TUNE LED now driven by byte 4 bit 6 of each RCU frame, 1.5s felt noticeably out of sync against the physical TUNE LED. 0.5s gives near-real-time tracking while staying well under the amp's serial-buffer saturation envelope. If new symptoms appear (cursor flicker, dropped frames, wedged buffer), back off to 1.0s before going further.
10. **Opt-in raw-byte capture for protocol investigation (2026-06-19)** — `SerialConfig.debug_raw_log: PATH` in `config.yaml` (empty = off, the default). When set, every chunk the serial reader receives is appended to that file as `<monotonic_seconds> <hex>\n` *before* the framer dispatches to CSV/RCU parsers — so we can see any frame types the parser currently drops (anything that isn't `AA AA AA 0x43 …` CSV or `AA AA AA 0x6A …` RCU). Investigation-only — leave off in production, the file grows unbounded. Added to support the TUNE-bit hunt: the 1.5K-FA's RCU LCD payload doesn't seem to expose a tune-in-progress bit (verified via `tools/find_tune_bit.py` in macexpert-spe — only screen-class flips between bypass-OP and standby-logo, no single-bit flag found), so we need to check whether the amp emits a different response type during a TUNE press that the current parser silently drops.
11. **Strict CSV parse: drop torn frames instead of misclassifying them as STBY/RX (2026-05-15)** — `parse_status` used to map `data[2]=="O"→Oper, else Stby` and `data[3]=="T"→TX, else RX`. At aggressive poll rates (the local `tx_interval: 0.1` workaround) a torn / shifted CSV frame would land an unexpected byte at one of those positions and silently produce `op_status="Stby"` mid-transmission, which MacExpert dutifully rendered as a STANDBY-banner flicker while the amp was happily in OPER+TX. New behaviour: validate `data[2] ∈ {O,S}` and `data[3] ∈ {T,R}` (after `.strip()` to tolerate the occasional whitespace padding) and return None with a WARNING log on anything else, so the bad frame just doesn't broadcast and the next clean poll picks up where we left off. If the WARN log shows hits during normal use, the underlying fix is to revert `tx_interval` to the 0.2 default — but the strict parser keeps the symptom invisible regardless. **Resolved 2026-06-19 — see item 4; both the Pi and the committed config are back at the 0.2 / 1.0 defaults.**
12. **`power_on` clients consolidated on the WebSocket path (2026-05-15)** — the Node-RED `vu2cpl-shack` flow used to run a separate Pi-side `exec` node that spawned `python3 /home/vu2cpl/power_spe_on.py` for its `ON_SPE` button, on the (incorrect) theory that the WS server couldn't wake a powered-off amp because the serial *data* link was dead. In fact `spe/power_control.py` `_power_on_sync()` does the same DTR/RTS sequence on the already-open serial handle — and the FTDI hardware lines are controllable via ioctl regardless of whether the amp's CPU is alive. **Correct client-side behaviour:** send the string `power_on` over WebSocket → the server toggles DTR (`DTR=1 → DTR=0 → RTS=1 → wait 1 s → DTR=1 → RTS=0`) → amp starts in 3–4.5 s → the server replies with a `power_result` JSON ack on the same socket. **Do not have other clients spawn `power_spe_on.py` while this service is running** — both would try to manipulate `/dev/ttyUSB0`, you'd hit the same FTDI-handle contention that the Serial-stack fixes (item 8 below) cleaned up. Standalone `power_spe_on.py` in this repo stays as a fallback tool only for the case where `spe-remote.service` itself is down. Documented in `vu2cpl-shack` CLAUDE.md + SHACK_CHANGELOG (commit `fa0a18d`).
13. **RCU counts toward heartbeat liveness** — `serial_handler` now stamps `_last_rcu_at` on every emitted RCU frame and exposes `last_rcu_age`. `presence_heartbeat_loop` reports `serial:"up"` if EITHER CSV or RCU is fresher than `amp_alive_threshold`. Fixes the "POWERED OFF banner appears in STANDBY" bug: the amp slows CSV in STANDBY below the 3 s threshold, but the RCU OFF→ON ticker forces fresh display frames every 1.5 s, so RCU liveness keeps the heartbeat honest. The Pi's local `config.yaml` had been tweaked to `tx_interval: 0.1` / `idle_interval: 0.4` as a workaround — that's still in effect but no longer load-bearing for this bug. **Update 2026-06-19 — see item 4; the Pi has since been reverted to the 0.2 / 1.0 defaults and the committed config now matches.**
14. **Live °C/°F toggle** (`de5c99e`, `1c638bc`) — temperature unit is now configurable via `config.yaml` (`amp.temperature_unit: C|F`) and toggleable from the dashboard; the server writes the change back to YAML so it survives restart. The SPE protocol returns temperatures unit-less, so the server has to be told which unit the front panel is set to.
15. **README rewrite** (`af38e82`) — leads with the multi-client architecture diagram and the systemd install path.
16. **systemd installer** (`617c8b3`) — `install-service.sh` / `uninstall-service.sh` plus a sample Node-RED flow.
17. **Field rename** (`fd01936`) — JSON `model` → `model_id` to line up with the MacExpert decoder.
18. **Auto-detect amp model** (`9c0daaf`, `58c389a`) — scans the first 3 CSV fields of the status string for the model ID pattern and adapts the UI; robust against firmware variants.
19. **Python 3.9 compatibility** (`cd427c0`) — needed because Raspberry Pi OS Bookworm ships 3.9.
20. **Serial-stack fixes** (`3f69277`, `e1eec59`, `4f2bf2e`, `c3e48b9`) — the painful run: don't open a second pyserial handle for power control (was wedging the FTDI), restored `port.flush()` with write back-pressure, stopped saturating the amp's serial buffer by slowing the RCU tick and not speed-polling in OPER. Resolved the freeze that the diagnostic logging exposed.
21. **Reverted** (`d6fe518`, `94fe469`) — the FTDI settle / multi-port auto-discovery experiment was rolled back; the simple single-port approach won.

## Architecture invariants — don't break these

- **Only one process opens the serial port.** If you ever find yourself adding a second `serial.Serial(...)` call, stop. The power-control path was the trap that caused `3f69277`.
- **Threaded reader + async writer.** Blocking reads run on a thread because `serial_asyncio` was crashing on USB-serial poll glitches. Don't "modernize" this back to pure asyncio without testing on a real Pi + FTDI.
- **Mixed-client broadcast on the same WS.** Text JSON for browsers, binary RCU frames for MacExpert. Same socket, dispatched by message type.
- **Python 3.9 floor.** No `match` statements, no `X | Y` type unions in runtime code. Bookworm Pi is the deployment target.
- **No saturating the amp.** TX-poll interval and RCU tick rate were tuned by trial; bumping them up will wedge the amp's serial buffer again.

## Power control — how the button actually works

Power ON and OFF use **different mechanisms** because the SPE protocol has no "power on" command — only "off". Documenting the full call path here so future-Manoj doesn't have to re-derive it from the code.

**Power ON** (hardware DTR/RTS toggle):

```
browser button
  → WS text frame "power_on"
  → spe/websocket_handler.py  _handle_power("power_on")
  → spe/power_control.py      PowerController.power_on()
  → run_in_executor → _power_on_sync()
  → ioctls on SerialHandler._port (the already-open FTDI handle):
      DTR=1 → DTR=0 → RTS=1 → sleep 1s → DTR=1 → RTS=0
  → amp boots in 3–4.5 s
  → server replies with {"power_result":"power_on","status":"ok"}
```

**Power OFF** (serial command, not hardware lines):

```
browser button
  → WS text frame "power_off"
  → _handle_power("power_off")
  → PowerController.power_off()
  → SerialHandler.send_command("power_off")
  → command byte 0x0A goes out on the same serial port (SWITCH OFF, per
    SPE Application Programmer's Guide Rev 1.1)
```

**Why `_power_on_sync` reaches into `SerialHandler._port` directly** instead of opening its own `serial.Serial(...)`: pyserial's `open()` reconfigures termios and toggles DTR/RTS as a side effect. The original code did open a second handle (mirroring OH2GEK's standalone `power_spe_on.py`) and it wedged the FTDI driver on the second power action in a session. The fix in commit `3f69277` was to assign `.dtr` / `.rts` directly on the existing fd — those are pure ioctls, no termios churn, no second handle. **If you ever find yourself "cleaning this up" by opening a fresh Serial(), re-read that commit first.**

**Side effects of the DTR sequence:**

- While DTR is held high, the amp's front-panel power switch is overridden. The front panel shows `POWER SWITCH HELD BY REMOTE`.
- The hardware lines work whether the amp's CPU is alive or not — that's the whole point. The 2026-05-15 client-consolidation work (Recent work item 1) was triggered by a Node-RED flow that wrongly assumed the WS path couldn't wake a powered-off amp because the *data* link was dead. The data link is irrelevant; DTR is a separate wire.

**Standalone `power_spe_on.py` at the repo root** is the OH2GEK original. Kept as a fallback for the case where `spe-remote.service` itself is down. **Do not invoke it while the service is running** — that's the second-pyserial-handle trap the architecture-invariant section warns about.

## Config (`config.yaml`)

- `serial.port` — uses the stable `/dev/serial/by-id/...` path, not `/dev/ttyUSB0`. Don't switch back; ttyUSB numbering changes across reboots.
- `polling.tx_interval` 0.2s, `idle_interval` 1.0s, `heartbeat` 15s — leave these alone unless you have a scope on the wire. The Pi runs locally-tweaked 0.1 / 0.4 values that are not in the default file.
- `polling.amp_alive_threshold` 3s — applies to the most recent of CSV state OR RCU display frame (see item 1 in Recent work). RCU OFF→ON ticks at 1.5s, so 3s is a generous margin while still flipping `serial:"down"` within ~3s of amp power-off (FTDI link survives, but no more RCU frames come back).
- `amp.temperature_unit` C or F — must match the SPE front-panel setting.
- `logging.level` INFO by default; DEBUG is loud (every serial write).

## Deployment

systemd unit `spe-remote.service` runs the server as a daemon. `install-service.sh` is the supported install path; the README tells users to clone the repo, run `setup.sh`, then `install-service.sh`. Service logs go to journald.

## Open threads / ideas (not started)

- No formal test suite. Everything has been tested against a live amp.
- Auth on the WebSocket — currently open on the LAN, fine for a shack network, would need a token for anything exposed.
- The dashboard's gauge rendering is canvas-based and hand-rolled; could move to a library but the current code is small and works on mobile.

## Where to pick up

Working tree is clean and pushed. Next session can start by reading this file and `git log --oneline -10` to confirm nothing landed in between. If a serial-stack bug reappears, re-read commits `3f69277`, `e1eec59`, `4f2bf2e` before changing anything — that whole sequence is the institutional memory for "don't touch the serial port lifecycle."
