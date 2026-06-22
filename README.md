# SPE Amplifier Remote Control

A modern Python 3 remote control server for **SPE Expert** HF amplifiers (1.3K-FA, 1.5K-FA, 2K-FA) with a built-in web interface. Runs on a Raspberry Pi and serves a real-time dashboard to any browser on your network.

![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

## Features

- **Power On/Off** — remote power control via DTR line (on) and serial command 0x0A (off)
- **Full SPE protocol** — all commands from the official Application Programmer's Guide Rev 1.1, plus undocumented RCU commands
- **RCU (Remote Control Unit) mode** — live LCD display mirror streamed as binary frames; compatible with the MacExpert companion app
- **Orchestrated TUNE + band sweep (multi-radio)** — drives a transmit rig to key a clean carrier while the SPE's ATU sweeps: a **FlexRadio 6000** over SmartSDR **or** an **Expert Electronics SunSDR / ExpertSDR3** over TCI. Runs the SM5TOG-style ATU tune flow (carrier on → watch RCU TUNE-LED bit → carrier off) and sweeps the SPE manual's full sub-band table on demand. The active rig is chosen by `radio.kind` and can be picked/configured live from the client. Triggerable from MacExpert's SWEEP panel, the bundled web dashboard's SWEEP button, the Node-RED `/ui` SPE panel, and the Vue `/shack` SPE card — all read the same `tune_event` broadcasts.
- **Client-selected radio** — the operator picks Flex vs SunSDR and edits host/port/etc from the client (a `RADIO` settings panel in the bundled dashboard); the change applies live on the Pi and persists to `config.yaml`. No SSH, no restart.
- **Flex auto-discovery** — leave `flex.host` empty in `config.yaml` and spe-remote listens for the SmartSDR UDP broadcast on port 4992; the radio's IP and model are picked up automatically. Static config still wins when set.
- **Self-contained** — single process serves both WebSocket API and web UI (no Apache/Nginx needed)
- **Multi-client** — multiple browsers/devices can monitor the amplifier simultaneously
- **Mixed-client broadcast** — text JSON for browsers, binary frames for RCU-capable clients, same socket
- **Real-time gauges** — SWR, drain current, PA temperature, voltage with canvas-based arc gauges
- **Responsive** — works on desktop, tablet, and mobile
- **Auto-reconnect** — WebSocket and serial both reconnect automatically on failure
- **Threaded reader + async writer** — blocking reads survive USB-serial poll glitches that crash `serial_asyncio`
- **Graceful shutdown** — SIGINT/SIGTERM handler cancels tasks and closes the port cleanly
- **Configurable** — YAML config file for serial port, baud rate, polling intervals
- **Guided setup** — `./configure.sh` lists the Pi's `/dev/serial/by-id/` ports to pick from, walks you through the optional Flex radio, and keeps your saved settings on re-run; `sudo ./install-service.sh --update` re-configures and restarts the live service with a diff preview

## How It Works — One Server, Many Clients

The Python server is the **only** process that opens `/dev/ttyUSB0`. Every UI on every device connects to it over a single WebSocket on port 8888 — so the bundled browser dashboard, Node-RED running locally on the Pi, and MacExpert running on your laptop can all monitor and control the amp **simultaneously** with no serial-port contention.

```
                        SPE Amplifier
                              │
                          USB / RS-232
                              │
                              ▼
                     ┌──────────────────┐
                     │  /dev/ttyUSB0    │   ← only one process ever opens this
                     └────────┬─────────┘
                              │
                              ▼
                ┌─────────────────────────────┐
                │   spe-remote Python server  │
                │   on Raspberry Pi  :8888    │
                │   (systemd: spe-remote)     │
                └──────┬────────┬──────┬──────┘
                       │        │      │
            ws://pi:8888/ws (text JSON state + commands)
                       │        │      │
        ┌──────────────┘        │      └──────────────┐
        ▼                       ▼                     ▼
┌────────────────┐    ┌───────────────────┐    ┌────────────────┐
│ Web dashboard  │    │ Node-RED on Pi    │    │ MacExpert      │
│ http://pi:8888 │    │ ws://localhost... │    │ on your Mac    │
│ (any browser)  │    │ flow + automation │    │ (LCD mirror)   │
└────────────────┘    └───────────────────┘    └────────────────┘
```

**Why this matters:**
- Old way (Node-RED holding the serial port directly) — only one app at a time could talk to the amp.
- New way — every client sees the same live state, and any one of them can send commands.
- All commands go through the same parser/queue inside the server, so they can't collide on the wire.

**Drop-in flows for each client:**
- Browser: served from the Pi at `http://<pi>:8888/` — no install.
- Node-RED: import `docs/nodered-spe-ws-flow.json` (see [Node-RED Integration](#node-red-integration)).
- MacExpert: native macOS app — uses the same WebSocket, plus the binary RCU LCD mirror.

## Web Interface

The web client displays:
- **Power ON / OFF** buttons with confirmation dialog and busy animation
- Power output bar (0–1500 W) with gradient
- SWR, drain current, temperature, and voltage gauges
- TX/RX status indicator (red pulse during TX, green during RX)
- Band, antenna, input, and power level information
- Warning and error alerts from the amplifier
- Control buttons: Operate, ANT, TUNE, INPUT, POWER, BAND +/−

## Requirements

- Raspberry Pi (any model) or any Linux/macOS/Windows machine
- Python 3.9+
- SPE Expert amplifier connected via USB or RS-232
- `python3-venv` package (on Debian/Raspberry Pi OS)

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/vu2cpl/spe-remote.git
cd spe-remote
```

### 2. Run Setup

```bash
./setup.sh
```

This creates a Python virtual environment and installs all dependencies. On Raspberry Pi OS, it will also install `python3-venv` if needed.

### 3. Serial Port Access

```bash
sudo usermod -aG dialout $USER
# Log out and back in for this to take effect
```

### 4. Configure

The easiest path is the **interactive configurator**, which `setup.sh` runs
for you automatically the first time. You can also run it any time:

```bash
./configure.sh
```

On a Raspberry Pi it **lists the serial-port aliases under `/dev/serial/by-id/`
and lets you pick the right one** — no need to hunt for the path by hand. It
then offers to set up the **optional Flex radio** (orchestrated TUNE + band
sweep); just answer **no** to skip it if you don't have a Flex. Anything you've
already configured is offered as the default, so **re-running never loses your
serial port or Flex IP** — press Enter to keep them. Nothing is written until
you've reviewed a diff and confirmed.

> The configurator only edits the host-specific keys (serial port and the
> `flex:` block) using comment-preserving in-place substitutions, so the rest
> of `config.yaml` — including all the explanatory comments — is left intact.

Prefer to edit by hand? `config.yaml` is plain YAML — here's the full shape:

```yaml
serial:
  port: /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_XXXXXXXX-if00-port0
  baudrate: 115200
  timeout: 1.0

server:
  port: 8888
  host: "0.0.0.0"

polling:
  tx_interval: 0.2      # Poll rate during TX (seconds)
  idle_interval: 1.0     # Poll rate during RX/Standby
  heartbeat: 15          # Force state push interval

amp:
  temperature_unit: C    # Must match the SPE setup-menu unit (C or F)

# Optional — only needed if you want orchestrated TUNE + band sweep.
# When `enabled: false` (the default), spe-remote runs exactly as before.
# See "Orchestrated TUNE and Band Sweep" below for details.
flex:
  enabled: false
  host: "192.168.1.148"   # Static LAN IP of your Flex 6000-series radio
  port: 4992              # SmartSDR TCP control port (default)
  slice_rx: 0             # Which slice to drive during tune cycles
  tune_power_watts: 10    # Carrier power for ATU sweeps (5–15 W typical)

logging:
  level: INFO
```

> **Temperature unit:** the SPE protocol returns temperatures unit-less — the amp doesn't tell us whether 33 means 33 °C or 33 °F. Set `amp.temperature_unit` to whichever your front-panel setup menu is configured for. The server stamps it onto every state update so the web client renders the correct symbol and scales the temperature gauge accordingly (0–80 in °C mode, 0–180 in °F mode).
>
> You can also flip it from the dashboard: there's a tiny **`→ °F`** / **`→ °C`** toggle next to the PA Temp gauge label. Clicking it sends `set_temp_unit:F` (or `:C`) over the WebSocket, the server updates in memory, **rewrites the line in `config.yaml`** so it survives restarts, and broadcasts the change to every connected client (browser + Node-RED + MacExpert) within a second. No SSH, no `systemctl restart`.

**Finding your serial port:**

```bash
# List USB serial devices
ls /dev/serial/by-id/

# Or check dmesg after plugging in
dmesg | grep ttyUSB
```

Using `/dev/serial/by-id/...` paths is recommended — they persist across reboots unlike `/dev/ttyUSB0`.

### 5. Install as a System Service (recommended)

```bash
sudo ./install-service.sh
```

That's it. The installer auto-detects your user and the install path, adds you to the `dialout` group if needed, and registers `spe-remote` with systemd so it starts on boot and restarts on failure.

Re-installing is safe: **`install-service.sh` never touches `config.yaml`**, so
your saved serial port and Flex IP are preserved across upgrades.

**Changing the serial port or Flex radio later** — use the `--update` flag. It
re-runs the interactive configurator (which keeps your current values unless you
change them), **shows a diff of what will change**, and then restarts the running
service so the new config takes effect:

```bash
sudo ./install-service.sh --update
```

Useful commands afterwards:

```bash
sudo systemctl status spe-remote      # is it running?
sudo ./install-service.sh --update    # change serial port / Flex IP, then restart
sudo systemctl restart spe-remote     # restart after a hand-edit of config.yaml
sudo journalctl -u spe-remote -f      # tail logs live
sudo ./uninstall-service.sh           # remove the service later
```

#### 5.1. Or run in foreground (for testing)

If you'd rather see logs in your terminal:

```bash
./run.sh
```

Or detached, logs to `nohup.out`:

```bash
nohup ./run.sh &
```

> **Heads up:** only run *one* instance at a time. If the systemd service is already running, stop it first (`sudo systemctl stop spe-remote`) before launching `./run.sh`, otherwise both will fight for the serial port.

### 6. Open the Web Interface

Navigate to `http://<your-pi-ip>:8888/` in any browser.

This is the bundled dashboard. To also drive the amp from Node-RED on the same Pi or from MacExpert on your Mac, see the [How It Works](#how-it-works--one-server-many-clients) diagram above and the [Node-RED Integration](#node-red-integration) section.

## Orchestrated TUNE and Band Sweep

spe-remote can drive a transmit rig to key a clean carrier while the SPE's ATU sweeps. Set `radio.kind` in `config.yaml` to pick the backend — `flex` (FlexRadio over SmartSDR), `tci` (Expert Electronics SunSDR / ExpertSDR3 over TCI), or `none` — and it exposes these additional WebSocket commands that any client (MacExpert, browser dashboard, Node-RED) can call:

| WS command | What it does |
|---|---|
| `radio_connect` | Open the rig connection. Sent when a client opens its Sweep menu, so the radio is ready by the time the operator hits Start. Idempotent. (`flex_connect` is a back-compat alias.) |
| `radio_disconnect` | Close the rig connection. Sent when a client closes its Sweep menu while idle. Ignored while a tune cycle is running. (`flex_disconnect` alias.) |
| `tune_single` | Run one ATU tune cycle on the rig's current freq. Sends SPE TUNE keycode, waits for the front-panel TUNE LED to come on (RCU byte 4 bit 6), keys the rig's tune carrier, waits for the LED to go off (ATU done), cuts the carrier. No blind timing. |
| `tune_band:<band>` | Sweep the SPE manual's recommended in-band sub-band centers for `<band>` (`160m`, `80m`, `60m`, …, `6m`). Saves the operator's pre-sweep VFO freq + mode, hits each sub-band in turn, restores the VFO at the end. |
| `tune_stop` | Abort an in-progress single tune or sweep. The carrier-off command runs in a `finally` block — a stopped cycle always drops the carrier before exiting. |
| `get_config` / `set_radio_config:<json>` | Read / live-change the active radio + its settings from the client. See [Client-driven radio config](#client-driven-radio-config). |

The tune **sequence is radio-agnostic** — only the per-rig commands differ:

| Step | Flex (SmartSDR) | SunSDR (TCI) |
|---|---|---|
| Set freq | `slice t <s> <MHz>` | `vfo:<trx>,0,<Hz>;` |
| Set mode | `slice s <s> mode=CWU` | `modulation:<trx>,CW;` |
| Tune carrier | `transmit tune on/off` | `tune:<trx>,true/false;` |

**On-demand connection (the radio is only held while tuning).** spe-remote does **not** open the rig session at startup. It connects when the operator opens the Sweep menu (`radio_connect`) and drops it again as soon as the tune cycle or band sweep finishes — so the radio isn't marked "in use" the rest of the time, and it can be powered off until you actually need it (host resolution is deferred too). As a safety net the server also connects lazily at the start of any `tune_single` / `tune_band`, so a client that never sends `radio_connect` still works. Connection transitions broadcast as `tune_event` phases `RADIO_CONNECTING` → `RADIO_CONNECTED` → `RADIO_DISCONNECTED` (or `RADIO_ERROR`).

### Client-driven radio config

The active rig and its settings live in `config.yaml` (`radio.kind` + a `flex:` and a `tci:` section), but a client doesn't need to touch the file or restart the service. Sending `get_config` returns the current radio config as `{"config_event":"radio","radio":{kind, flex:{…}, tci:{…}}}`; sending `set_radio_config:<json>` (e.g. `{"kind":"tci","tci":{"host":"127.0.0.1","port":50001}}`) switches/edits the rig **live** — spe-remote disconnects the old rig, rebuilds the backend, rewrites `config.yaml` (preserving comments), and broadcasts the new config. The bundled dashboard's **RADIO** button is a working example. Changes are refused while a tune is running. Full contract: [`docs/CLIENT_RADIO_CONFIG.md`](docs/CLIENT_RADIO_CONFIG.md).

**SunSDR / TCI notes.** TCI is a WebSocket text protocol (default port 50001). `tci.trx` selects which receiver to key; tune power is left to ExpertSDR unless `tci.tune_drive` (percent) is set. Command set verified against the [sm5tog/sm5k-spe-tuner](https://github.com/sm5tog/sm5k-spe-tuner) reference.

Phase progress streams back to every connected client as JSON broadcasts on the same WS:

```json
{"tune_event": "SWEEP_STARTED", "tune_message": "20m: 7 sub-bands (14.025–14.325 MHz)", "ts": 1781867...}
{"tune_event": "VFO_SAVED",     "tune_message": "slice 0: 7.007200 MHz LSB"}
{"tune_event": "SWEEP_STEP",    "tune_message": "1/7: 14.0250 MHz"}
{"tune_event": "STARTED",       "tune_message": "freq=14.025"}
{"tune_event": "LED_ON",        "tune_message": ""}
{"tune_event": "CARRIER_ON",    "tune_message": "Flex 10W"}
{"tune_event": "LED_OFF",       "tune_message": "ATU done"}
{"tune_event": "CARRIER_OFF",   "tune_message": ""}
{"tune_event": "SUCCESS",       "tune_message": "cycle complete"}
... (next sub-band)
{"tune_event": "SWEEP_DONE",    "tune_message": "7/7 sub-bands tuned on 20m"}
{"tune_event": "VFO_RESTORED",  "tune_message": "slice 0: 7.007200 MHz LSB"}
```

Four UIs render the same broadcast stream as a sweep panel:

| UI | URL / app | Implementation |
|---|---|---|
| MacExpert app | macOS native | SwiftUI `SweepPanelView` modal — band picker, progress, Stop |
| Bundled web dashboard | `http://<pi>:8888/` | SWEEP button next to TUNE; inline panel in the controls row |
| Node-RED `/ui` | `http://<pi>:1880/ui` SPE tab | SWEEP button on the SPE Panel; collapsible panel below |
| Vue `/shack` | `http://<pi>/shack` SPE card | SWEEP in the 4-button controls grid; expandable panel inside the card |

All four send `tune_band:<band>` / `tune_stop` over the same WS and consume the same `tune_event` JSON, so the Pi-side orchestrator is the single source of truth. The bundled web dashboard also sends `flex_connect` / `flex_disconnect` as its Sweep panel opens and closes; the other clients can adopt those for a faster first tune, but don't have to — the server connects lazily at tune start regardless.

### Flex auto-discovery

When `flex.host` is empty (or omitted) in `config.yaml`, spe-remote listens on UDP port 4992 for the SmartSDR discovery broadcast that every Flex 6000-series radio emits ~1 Hz. The first packet that arrives during a 5 s window provides the radio's IP, model, callsign, and nickname; spe-remote logs them and connects:

```
Flex: flex.host empty — listening for SmartSDR discovery broadcast on UDP 4992 (up to 5s)…
Flex: discovered FLEX-6600 "6600" (VU2CPL) at 192.168.1.148
Flex: connected (version='1.4.0.0', handle='...')
```

If you want to pin a specific radio (multi-Flex shack) or skip the 5 s discovery wait, set `flex.host` explicitly — the static value always wins.

### Band table

The sub-band centers come from the SPE 1.5K-FA User Manual rev 3.2, Section 19 — 154 entries across 11 bands. spe-remote filters to in-amateur-band only by default (`HAM_BAND_EDGES` in `spe/spe_band_table.py`) so a sweep doesn't hit out-of-band freqs the rig refuses to TX. 60m has a special override because the manual's list predates the WRC-15 amateur 60m allocation; it sweeps the single freq at 5.357 MHz instead.

### Diagnostic / experimental tools

- `python3 -m spe.flex_cli --allow-tx` — interactive SmartSDR command-line driver. Defaults to read-only (`slice list`, `transmit info`); `--allow-tx` unlocks anything that could key the rig. Tail subscribed events with `--watch`.
- `python3 -m spe.flex_carrier_test` — single-shot 10 W carrier test: connects, sets freq, keys for 3 s, unkeys. For verifying basic Flex connectivity without invoking the orchestrator.

### Constraints

- **One rig per Pi.** spe-remote talks to one Flex; multi-rig setups need separate config / Pi.
- **Operator picks band + antenna.** Per the SPE manual's procedure: select band, choose antenna with `[ANT]`, *then* trigger the sweep. spe-remote does not change band or antenna.
- **Ethernet interlock not used.** SmartSDR's `interlock create type=AMP` is rejected by older firmware (1.4.0.0 in our test rig); direct `transmit tune on` works fine without it. Re-evaluate when newer firmware is in play.

## SPE Serial Protocol

Based on the **SPE Application Programmer's Guide Rev 1.1** for Expert 1.3K-FA / 1.5K-FA / 2K-FA.

### Packet Format

```
0x55 0x55 0x55 [CNT] [DATA...] [CHK]
```

For single-byte commands: `CNT=0x01`, `CHK=DATA` (same byte).

### Command Set

All commands below are sent to the amp via WebSocket text messages — clients just send the bare name (e.g. `oper`). The Python server wraps them in the SPE packet format and writes them to the serial port.

| Hex  | Command        | WebSocket msg  | Description |
|------|----------------|----------------|-------------|
| 0x01 | INPUT          | `input`        | Toggle input port |
| 0x02 | BAND −         | `band_dn`      | Band down |
| 0x03 | BAND +         | `band_up`      | Band up |
| 0x04 | ANTENNA        | `antenna`      | Cycle TX antenna |
| 0x05 | L−             | `l_minus`      | ATU inductance minus |
| 0x06 | L+             | `l_plus`       | ATU inductance plus |
| 0x07 | C−             | `c_minus`      | ATU capacitance minus |
| 0x08 | C+             | `c_plus`       | ATU capacitance plus |
| 0x09 | TUNE           | `tune`         | Start ATU tuning |
| 0x0A | SWITCH OFF     | `power_off`    | Power OFF amplifier |
| 0x0B | POWER          | `power_level`  | Toggle power level (L/M/H) |
| 0x0C | DISPLAY        | `display`      | Display toggle |
| 0x0D | OPERATE        | `oper`         | Toggle Operate/Standby |
| 0x0E | CAT            | `cat`          | CAT mode |
| 0x0F | LEFT ARROW     | `left`         | Menu navigation left |
| 0x10 | RIGHT ARROW    | `right`        | Menu navigation right |
| 0x11 | SET            | `set`          | Menu enter/set |
| 0x80 | RCU ON         | `rcu_on`       | Enable live LCD mirror stream (undocumented) |
| 0x81 | RCU OFF        | `rcu_off`      | Disable live LCD mirror stream (undocumented) |
| 0x82 | BACKLIGHT ON   | `backlight_on` | Turn backlight on |
| 0x83 | BACKLIGHT OFF  | `backlight_off`| Turn backlight off |
| 0x90 | STATUS         | (auto)         | Request status string |

> **Note:** `rcu_on` / `rcu_off` are not in the official Programmer's Guide. They were reverse-engineered from the KTerm application traffic and are used internally by the server to drive the RCU LCD mirror — see the RCU section below.

### Response Frames

The amp multiplexes two response types on the same byte stream. Both are framed by three `0xAA` sync bytes, then a marker byte:

| Marker | Type       | Length | Description |
|--------|------------|--------|-------------|
| 0x43   | CSV status | 67 bytes + checksum + CRLF | ASCII comma-separated status string (see below) |
| 0x6A   | RCU frame  | Variable  | Proprietary binary LCD display payload; ends at next sync or quiet period |

The serial handler parses both inline, dispatching CSV frames to `on_state_update` and RCU frames to `on_rcu_frame`.

### Power On/Off

| Action     | Method | Notes |
|------------|--------|-------|
| **Power ON** | DTR hardware line toggle | No serial command exists; uses DTR/RTS sequence via USB-serial adapter |
| **Power OFF** | Serial command `0x0A` | SWITCH OFF — equivalent to pressing the front-panel OFF button |

> **Note:** When DTR is held high, it takes power mastering control — the amplifier shows "POWER SWITCH HELD BY REMOTE" warning and the front-panel power switch is overridden. Startup takes 3–4.5 seconds.

### Status String

The amplifier returns a 67-character ASCII comma-separated status string with 19 fields:

| Field | Contents |
|-------|----------|
| ID | `20K` (2K-FA) or `13K` (1.3K-FA) |
| Standby/Operate | `S` or `O` |
| RX/TX | `R` or `T` |
| Memory Bank | `A`, `B`, or `x` |
| Input | `1` or `2` |
| Band | `00` (160m) to `11` (4m) |
| TX Antenna + ATU | `0`–`6`, with `t`/`b`/`a` suffix |
| RX Antenna | Antenna number or `0r` |
| Power Level | `L`, `M`, or `H` |
| Output Power | Watts (4 chars) |
| SWR ATU | VSWR before ATU |
| SWR ANT | VSWR at antenna |
| V PA | Supply voltage |
| I PA | Drain current |
| Temp (upper) | Heatsink temp °C |
| Temp (lower) | Lower heatsink (2K-FA only) |
| Temp (combiner) | Combiner temp (2K-FA only) |
| Warnings | Single char code (see below) |
| Alarms | Single char code (see below) |

**Warning codes:** `M`=Alarm, `A`=No antenna, `S`=SWR, `B`=No band, `P`=Power limit, `O`=Overheat, `Y`=ATU N/A, `W`=Tune no power, `K`=ATU bypass, `R`=Remote hold, `T`=Combiner heat, `C`=Combiner fault, `N`=None

**Alarm codes:** `S`=SWR limit, `A`=Amp protection, `D`=Overdrive, `H`=Excess heat, `C`=Combiner fault, `N`=None

## RCU (Remote Control Unit) Mode

RCU is a streaming mode that mirrors the amplifier's front-panel LCD display over the serial link. When enabled, the amp emits binary frames (marker `0x6A`) every time the display changes, in addition to the regular CSV status polling.

### How the Server Uses RCU

1. On serial connect, the handler sends `CMD_REQUEST` (status) followed by `CMD_RCU_ON`.
2. A background task cycles `RCU_OFF` → `RCU_ON` every 500 ms to keep the stream alive — the amp sometimes stops emitting after a long quiet period.
3. A quiet-flush task force-terminates any half-received RCU frame after 300 ms of silence so static screens (no display changes) still emit their final frame.
4. Incoming RCU frame payloads are passed to the registered `on_rcu_frame` callback, which broadcasts them as **binary** WebSocket messages.

### Why Two Frame Types?

- **CSV status** — the machine-readable data (power, SWR, temps, warnings). Parsed into JSON and sent as WebSocket text messages. This is what the bundled browser dashboard consumes.
- **RCU frames** — the pixel-level view of the LCD. Lets a native client render an exact replica of the amplifier's front panel (including menu screens, settings, and power-on animations that aren't in the CSV).

### Clients

| Client       | Platform | Uses CSV | Uses RCU | Notes |
|--------------|----------|----------|----------|-------|
| Web dashboard | Browser | Yes      | No (drops binary) | Bundled in `web/` — opens at `http://<pi>:8888/` |
| MacExpert     | macOS   | Yes      | Yes      | Native Swift app; shares the same WebSocket contract |

The server broadcasts both frame types to all connected clients. Clients that don't know about RCU simply ignore binary messages.

### Sharing the Command Contract with MacExpert

The `COMMANDS` dict keys in `spe/protocol.py` are the canonical WebSocket command names. MacExpert's `SPEProtocol.swift` maintains a matching `wsCommandName` enum so both clients drive the amplifier identically. When adding a new command, update both sides.

## Architecture

### Threading Model (`serial_handler.py`)

The serial handler uses a hybrid thread + asyncio model instead of `pyserial-asyncio`:

```
┌────────────────────────────────────────────────────────────┐
│  Asyncio event loop (main thread)                          │
│   ├─ _poll_loop     ← periodic status requests             │
│   ├─ _command_loop  ← drains command queue, writes to port │
│   ├─ _rcu_tick_loop ← keeps RCU stream alive               │
│   ├─ _quiet_flush   ← flushes stalled RCU frames           │
│   ├─ _connection_watchdog                                   │
│   └─ Frame parser   ← drains receive buffer                │
│                                                             │
│      ▲                          │                           │
│      │ call_soon_threadsafe     │ _safe_write (with lock)   │
│      │                          ▼                           │
│  ┌──────────────────────────────────────────┐              │
│  │  Daemon thread: blocking serial.read()   │              │
│  │  → pushes raw chunks into asyncio queue  │              │
│  └──────────────────────────────────────────┘              │
└────────────────────────────────────────────────────────────┘
```

**Why not `serial_asyncio`?** Its internal `_read_ready` callback raises `SerialException("readiness to read but returned no data")` on Linux USB-serial adapters under moderate traffic. That bounces the port and breaks the RCU stream. The blocking `serial.Serial.read()` path used here never hits that bug, so the port stays up even under full RCU load.

**Writes** go through `_safe_write()`, which is guarded by a `threading.Lock` so the async command loop never interleaves packets with the RCU ticker.

### Key Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `_RCU_TICK_INTERVAL` | 0.5 s | RCU OFF → ON cycle cadence |
| `_RCU_OFF_ON_GAP` | 0.05 s | Gap between RCU OFF and ON |
| `_RCU_QUIET_FLUSH` | 0.3 s | Force-flush stuck RCU frames after this much silence |
| `_READ_TIMEOUT` | 0.1 s | Blocking serial read timeout |
| `_MAX_BUFFER` | 4096 bytes | Receive buffer cap before discarding stale bytes |

### Lifecycle

1. `server.py` creates `SerialHandler`, `PowerController`, and the Tornado app.
2. Installs SIGINT/SIGTERM handlers that schedule `serial_handler.stop()` on the asyncio loop, then stop both Tornado and asyncio loops.
3. `serial_handler.start()` loops: open port → spawn reader thread → run the five asyncio tasks via `asyncio.wait(FIRST_COMPLETED)` → tear down on any task exit → reconnect after 3 s.
4. On `stop()`: sets `_stop_reader` event, sends `RCU_OFF` to the amp, closes the port, drops queued commands. The reader thread exits naturally once its port handle is closed.

## Service Internals

The Quick Start covers the basic install (`sudo ./install-service.sh`). This section is for what's inside the unit if you want to tweak it.

The unit is rendered from `systemd/spe-remote.service.template` and dropped at `/etc/systemd/system/spe-remote.service`. Highlights:

| Setting | Why |
|---|---|
| `Type=simple` | The Python server doesn't fork; it stays in the foreground. |
| `KillSignal=SIGTERM` + `TimeoutStopSec=10` | Lets the server's shutdown handler run — closes WebSocket clients with a proper close frame, stops keepalive, releases the serial port. Forced kill only after 10 s. |
| `Restart=always` / `RestartSec=5` | Auto-recovers from crashes or transient serial errors. |
| `After=network-online.target` | Starts after the Pi has its IP, so `host: 0.0.0.0` actually has interfaces to bind. |
| `User=<you>` / `Group=<you>` | Drops privileges. Must be in the `dialout` group for serial access — installer handles this. |
| `NoNewPrivileges=true`, `ProtectSystem=full`, `ProtectHome=read-only` | Modest hardening; none of these block serial or WebSocket I/O. |
| `StandardOutput/Error=journal` | Logs go to journald. View with `journalctl -u spe-remote -f`. |

After editing the template, re-run `sudo ./install-service.sh` to re-render and reload.

## Project Structure

```
spe-remote/
├── config.yaml                    # Configuration file
├── requirements.txt               # Python dependencies
├── setup.sh                       # One-time setup (creates venv, installs deps)
├── run.sh                         # Foreground start
├── install-service.sh             # systemd installer (auto-detects user/path)
├── uninstall-service.sh           # systemd uninstaller
├── server.py                      # Main entry point (signal-safe shutdown)
├── power_spe_on.py                # Original OH2GEK power-on script (reference)
├── spe/
│   ├── __init__.py
│   ├── config.py                  # YAML config loader
│   ├── protocol.py                # SPE commands, response markers, state parser
│   ├── power_control.py           # Power on (DTR) / off (0x0A) controller
│   ├── serial_handler.py          # Thread reader + asyncio writer, CSV+RCU framing
│   ├── websocket_handler.py       # Multi-client text+binary broadcast + keepalive
│   └── app.py                     # Tornado app + no-cache static handler
├── web/                           # Bundled browser dashboard (text JSON only)
│   ├── index.html
│   ├── style.css
│   └── app.js
├── systemd/
│   └── spe-remote.service.template
└── docs/
    ├── SPE_Remote_Control_User_Guide.pdf
    ├── generate_guide.py          # PDF generator script
    └── nodered-spe-ws-flow.json   # Sample Node-RED flow (WebSocket-based)
```

## Node-RED Integration

A ready-to-import Node-RED flow lives in `docs/nodered-spe-ws-flow.json`. It connects to this server's WebSocket on `ws://localhost:8888/ws` (so Node-RED, the browser dashboard, and MacExpert can all run at the same time without serial-port contention).

To import: Node-RED ☰ menu → Import → paste the file contents → Import. The flow adds a new dashboard group called "Amplifier (WS)" with the same buttons and gauges as the bundled web client.

## WebSocket API

Connect to `ws://<host>:8888/ws`

The socket carries **three** kinds of server-to-client messages: JSON state updates (text), JSON power-action results (text), and raw RCU LCD frames (binary). Clients that don't care about RCU should ignore binary messages.

### Server → Client

**1. Amplifier state (text JSON, broadcast on change or heartbeat):**

```json
{
  "model_id": "20K",
  "op_status": "Oper",
  "tx_status": "TX",
  "input": "1",
  "band": "80m",
  "tx_antenna": "1",
  "p_level": "H",
  "p_out": "1353",
  "swr": "1.54",
  "aswr": "1.12",
  "voltage": "54.6",
  "drain": "27.3",
  "pa_temp": "26",
  "pa_temp_lower": "24",
  "pa_temp_combiner": "23",
  "temperature_unit": "C",
  "warnings": "",
  "error": ""
}
```

`model` is the amp's own ID code: `"13K"` (Expert 1.3K-FA), `"15K"` (1.5K-FA), or `"20K"` (2K-FA). The web client uses it to set the page header, scale the power bar (1500 W vs 2000 W), and show the lower-heatsink + combiner temps that only the 2K-FA reports. Empty string = unknown/not yet received.

**2. Power action result (text JSON, sent after `power_on` / `power_off`):**

```json
{
  "power_result": "power_on",
  "status": "ok"
}
```

`status` is `"ok"` on success, `"error"` on failure (check server logs for details).

**3. RCU LCD frame (binary, broadcast whenever the amp display changes):**

The payload is the raw bytes **after** the `AA AA AA 6A` sync+marker — i.e. the content portion of the RCU frame only. Decoding this into a pixel buffer is client-specific; see MacExpert's `RCUFrameDecoder.swift` for a reference implementation.

### Client → Server

Clients send bare command names as WebSocket text messages. The server dispatches:

- `power_on` → `PowerController.power_on()` (DTR hardware toggle)
- `power_off` → `PowerController.power_off()` (serial command `0x0A`)
- Everything else → `SerialHandler.send_command()` → serial write

**Full command list:**

| Command        | Action                          |
|----------------|---------------------------------|
| `power_on`     | Power ON via DTR toggle         |
| `power_off`    | Power OFF via serial cmd 0x0A   |
| `oper`         | Toggle Operate/Standby          |
| `antenna`      | Cycle TX antenna                |
| `input`        | Toggle input port               |
| `tune`         | Start ATU tuning                |
| `power_level`  | Toggle power level (L/M/H)      |
| `band_up`      | Band up                         |
| `band_dn`      | Band down                       |
| `l_plus`       | ATU inductance +                |
| `l_minus`      | ATU inductance −                |
| `c_plus`       | ATU capacitance +               |
| `c_minus`      | ATU capacitance −               |
| `display`      | Toggle display                  |
| `cat`          | CAT mode                        |
| `left`         | Menu navigation left            |
| `right`        | Menu navigation right           |
| `set`          | Menu enter/set                  |
| `rcu_on`       | Enable RCU LCD mirror stream    |
| `rcu_off`      | Disable RCU LCD mirror stream   |
| `backlight_on` | Backlight on                    |
| `backlight_off`| Backlight off                   |
| `set_temp_unit:C` / `:F` | Switch temperature display unit live; persisted to `config.yaml` |

> **Alias:** `gain` is kept as an alias for `power_level` for backward compatibility with the original OH2GEK client.

**Radio tune/sweep commands** (when `radio.kind` is `flex` or `tci` — see [Orchestrated TUNE and Band Sweep](#orchestrated-tune-and-band-sweep)):

| Command | Action |
|---|---|
| `radio_connect` | Open the rig connection (on Sweep-menu open). Idempotent. (`flex_connect` alias.) |
| `radio_disconnect` | Close it (on idle Sweep-menu close). Ignored mid-tune. (`flex_disconnect` alias.) |
| `tune_single` | One ATU tune cycle at the rig's current freq |
| `tune_band:<band>` | Sweep the manual's sub-bands for `<band>` (e.g. `tune_band:20m`) |
| `tune_stop` | Abort an in-progress tune/sweep (always drops the carrier) |
| `get_config` | Reply with the current radio config (`config_event:"radio"`) |
| `set_radio_config:<json>` | Switch/edit the active radio live + persist (see [`docs/CLIENT_RADIO_CONFIG.md`](docs/CLIENT_RADIO_CONFIG.md)) |

Progress streams back as `{"tune_event": <phase>, "tune_message": <text>, "ts": <t>}` — see the linked section for the full phase vocabulary, including the `RADIO_CONNECTING` / `RADIO_CONNECTED` / `RADIO_DISCONNECTED` connection-lifecycle phases.

### Example: JavaScript Client

```javascript
const ws = new WebSocket("ws://<pi>:8888/ws");

ws.onmessage = (evt) => {
  if (typeof evt.data === "string") {
    const msg = JSON.parse(evt.data);
    if (msg.power_result) { /* handle power action result */ }
    else                  { /* handle state update */ }
  } else {
    // Binary message = RCU LCD frame.
    // evt.data is a Blob; convert to ArrayBuffer to decode.
    evt.data.arrayBuffer().then(buf => renderRCU(new Uint8Array(buf)));
  }
};

ws.send("oper");        // toggle Operate
ws.send("band_up");     // band up
ws.send("power_off");   // power OFF via 0x0A
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `Serial error: [Errno 2] No such file` | Check serial port path in `config.yaml` |
| `Permission denied: /dev/ttyUSB0` | Add user to dialout group: `sudo usermod -aG dialout $USER` then re-login |
| Web page not loading | Check firewall: `sudo ufw allow 8888/tcp` |
| Gauges not updating | Check browser console for WebSocket errors |
| Multiple `/dev/ttyUSBx` devices | Use `/dev/serial/by-id/...` path instead |
| Power ON not working | Check FTDI USB-serial adapter supports DTR — verify with `dmesg` |
| "POWER SWITCH HELD BY REMOTE" | Normal when DTR is held high after power on |
| Logs show "Suppressed spurious USB-serial poll glitch" | Harmless — the kernel lies about poll readiness on USB-serial; the reader thread handles it |
| RCU frames never arrive | Check the companion client actually reads binary WebSocket messages; the browser dashboard doesn't |
| Server doesn't exit on Ctrl+C | Should never happen with the new shutdown handler — if it does, check for a hung serial read and file an issue |
| MacExpert can't send commands | Verify `wsCommandName` enum in Swift matches `COMMANDS` keys in `spe/protocol.py` |

## Credits

- **Original script**: [OH2GEK](https://github.com/oh2gek/SPE-1.3-2K-FA-Remote-server) — Python 2 server with WebSocket interface for SPE amplifiers
- **Modernized version**: VU2CPL — Python 3 port with async I/O, multi-client support, power on/off, full SPE protocol, RCU LCD mirror, threaded serial reader, built-in web client, and responsive UI
- **Native macOS companion app**: MacExpert — Swift client that decodes RCU binary frames to render a pixel-accurate LCD mirror, shares the WebSocket command contract with the bundled web client
- **Protocol reference**: SPE Application Programmer's Guide Rev 1.1 for Expert 1.3K-FA / 2K-FA (RCU commands reverse-engineered from KTerm)

## License

MIT License — see [LICENSE](LICENSE) for details.
