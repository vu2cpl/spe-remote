# Client integration spec — multi-radio tune + client-selected radio config

Target audience: client apps that drive spe-remote's orchestrated TUNE / band
sweep — **MacExpert** (native macOS), the bundled web dashboard, Node-RED, the
Vue `/shack` card. This documents the WebSocket contract added by the
multi-radio work so a client can (a) drive a Flex **or** a SunSDR/TCI rig
transparently, and (b) let the operator pick & configure the radio at runtime.

The bundled web dashboard already implements all of this (`web/app.js`,
`web/index.html`) — use it as the reference.

## Background

spe-remote now drives the tune rig through a generic backend chosen by
`radio.kind` on the Pi: `flex` (FlexRadio/SmartSDR), `tci` (ExpertSDR3 / SunSDR
over the TCI WebSocket protocol), or `none`. The connection is **on-demand**
(opened on Sweep-menu open / tune start, closed when the cycle ends) and the
active radio can be **changed live by a client** — no restart.

## WebSocket commands (client → server)

Send as plain text WS messages (same socket as everything else, `ws://<pi>:8888/ws`).

| Command | When | Effect |
|---|---|---|
| `radio_connect` | Sweep menu opens | Pre-warm the radio connection. Idempotent. (Old alias: `flex_connect`.) |
| `radio_disconnect` | Sweep menu closes while idle | Drop the connection. Ignored mid-tune. (Old alias: `flex_disconnect`.) |
| `get_config` | On connect, and when opening the radio settings UI | Server replies with the current radio config (see below). |
| `set_radio_config:<json>` | Operator applies a radio choice / settings edit | Switch/edit the radio live + persist to `config.yaml`. Refused while a tune runs. |
| `tune_single` / `tune_band:<band>` / `tune_stop` | unchanged | Drive a tune cycle / sweep / abort. |

### `set_radio_config:<json>` payload

JSON after the `:`; send only the section for the chosen kind.

```json
{"kind": "tci", "tci": {"host": "127.0.0.1", "port": 50001, "trx": 0, "mode": "CW", "tune_drive": 0}}
```
```json
{"kind": "flex", "flex": {"host": "192.168.1.148", "port": 4992, "slice_rx": 0, "tune_power_watts": 10}}
```
```json
{"kind": "none"}
```
- `kind` ∈ `flex | tci | none`. Unknown values are rejected with `RADIO_ERROR`.
- All section fields are optional; omitted fields keep their stored value.
- `host` empty for `flex` ⇒ UDP auto-discovery.
- Fields are **range-checked server-side** before anything is applied or
  written; a value outside its range is refused whole (nothing is applied,
  nothing is persisted) with `RADIO_ERROR` naming the field:

  | Field | Accepted |
  |---|---|
  | `flex.port`, `tci.port` | 1–65535 |
  | `flex.slice_rx` | 0–7 |
  | `flex.tune_power_watts` | 1–100 (the SPE wants 2–15 W) |
  | `tci.trx` | 0–1 |
  | `tci.tune_drive` | 0–100 (percent; 0 ⇒ leave it to ExpertSDR) |
  | `flex.host`, `tci.host` | a single token, ≤255 chars (empty ⇒ discover, flex only) |
  | `tci.mode` | alphanumeric, ≤10 chars; upper-cased |

## Server → client messages

### Radio config snapshot — `config_event: "radio"`
Sent in reply to `get_config`, and broadcast to all clients after a successful
`set_radio_config`. Use it to populate the radio picker / settings form.

```json
{
  "config_event": "radio",
  "persisted": true,
  "radio": {
    "kind": "flex",
    "flex": {"host": "192.168.1.148", "port": 4992, "slice_rx": 0, "tune_power_watts": 10},
    "tci":  {"host": "127.0.0.1", "port": 50001, "trx": 0, "mode": "CW", "tune_drive": 0}
  }
}
```

`persisted: false` means the change is live **but could not be written to
`config.yaml`** (read-only mount, permissions, file missing) — it reverts on
the next restart. A `RADIO_ERROR` with the reason follows the
`RADIO_CONFIG_UPDATED`; surface it rather than showing a plain success.

### Tune/connection events — `tune_event` (unchanged channel)
`{"tune_event": "<PHASE>", "tune_message": "...", "ts": <epoch>}`. Existing tune
phases are unchanged (STARTED, PREFLIGHT_OK, VFO_SAVED, FREQ_SET, TUNE_SENT,
LED_ON, CARRIER_ON, LED_OFF, CARRIER_OFF, VFO_RESTORED, SUCCESS, FAIL, ABORT,
SWEEP_STARTED, SWEEP_STEP, SWEEP_DONE). **New phases:**

| Phase | Meaning |
|---|---|
| `RADIO_CONNECTING` | Opening the rig connection (or discovering a Flex). |
| `RADIO_CONNECTED` | Connected; message carries kind + host + version. |
| `RADIO_DISCONNECTED` | Connection closed (housekeeping after a cycle). |
| `RADIO_ERROR` | Connect/config failed; message says why (e.g. radio off). |
| `RADIO_CONFIG_UPDATED` | A `set_radio_config` was applied (radio switched). The message says "this session only, not saved" when the config write failed — see `persisted` above. |

**Client handling:** treat `RADIO_*` like the old `FLEX_*` — they are *not*
tune progress. Don't flip sweeping state on them; surface `RADIO_ERROR` to the
user; ignore `RADIO_DISCONNECTED` / `RADIO_CONFIG_UPDATED` in the sweep status
(handle `RADIO_CONFIG_UPDATED`'s effect via the `config_event` message instead).
The phase string is open-ended — latch on the well-known terminals
(SUCCESS / FAIL / ABORT / SWEEP_DONE) and treat anything unknown as info.

## MacExpert UX guidance (for the client implementation)

- **Backward-compat first:** rename the existing `flexConnect()`/`flexDisconnect()`
  sends to `radio_connect`/`radio_disconnect` (the server accepts both), and
  extend the `FLEX_*` handling in `handleTuneEvent` to also match `RADIO_*`
  (keep `FLEX_*` for older servers). This alone keeps MacExpert working against
  the new server with no UI change.
- **Radio settings sheet:** a small settings sheet (gear button, or a section in
  the existing Settings) that:
  1. on appear, sends `get_config` and renders the `config_event:"radio"` reply;
  2. shows a segmented picker **None / FlexRadio / SunSDR (TCI)** bound to `kind`;
  3. shows the fields for the selected kind (Flex: host, port, slice, tune W;
     TCI: host, port, trx, mode, tune %);
  4. an **Apply** button sends `set_radio_config:<json>` with just the chosen
     section; disable Apply while `vm.isSweeping`.
- **Model:** add a `RadioConfig` Decodable mirroring the JSON above; store the
  last snapshot on the view model so the sheet and the Sweep panel can show
  which rig is active. Surface `RADIO_ERROR` in the existing error banner (as the
  on-demand work already does for `FLEX_ERROR`).
- The Sweep panel's `canStart` check is unchanged (WS mode + connected); the rig
  kind is transparent to it.

## Notes / constraints

- The WS is unauthenticated on the LAN (same trust model as the existing live
  `set_temp_unit` config write). `set_radio_config` rewrites `config.yaml` on the
  Pi (comment-preserving) and is refused while a tune is running. A failed write
  is reported, not swallowed (`persisted: false` + `RADIO_ERROR`).
- One rig at a time. Switching kind disconnects the current rig first.
- TCI tune power: ExpertSDR owns it unless `tci.tune_drive` (percent) is set > 0.
