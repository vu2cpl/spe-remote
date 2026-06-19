#!/bin/bash
# Interactive configurator for spe-remote's config.yaml.
#
# - On a Raspberry Pi (or anywhere /dev/serial/by-id exists) it lists the
#   stable serial-port aliases and lets you pick the right one.
# - It then offers to configure the optional Flex radio (orchestrated TUNE
#   + band sweep) — which you can skip.
# - Existing values are offered as defaults: press Enter to keep them. So
#   re-running this never loses the port or the Flex IP you set before.
# - Nothing is written until you've seen a diff and confirmed.
#
# Run it directly any time:   ./configure.sh
# setup.sh runs it on first install; install-service.sh --update runs it too.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PY="$DIR/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
    echo "Python not found (run ./setup.sh first to create the venv)." >&2
    exit 1
fi
TOOL="$DIR/configtool.py"

# Read a current value from config.yaml (empty string if unset/missing).
cur() { "$PY" "$TOOL" get "$1" 2>/dev/null || true; }

echo "=== spe-remote configuration ==="
echo

# ---------------------------------------------------------------------------
# Serial port
# ---------------------------------------------------------------------------
cur_port="$(cur serial.port)"
new_port="$cur_port"
# Stable per-device aliases live here on Linux/Raspberry Pi OS. Overridable
# via SPE_BYID_DIR so the flow can be exercised off-Pi.
BYID="${SPE_BYID_DIR:-/dev/serial/by-id}"

if [ -d "$BYID" ] && [ -n "$(ls -A "$BYID" 2>/dev/null)" ]; then
    echo "Serial devices found under $BYID:"
    ports=()
    while IFS= read -r p; do
        [ -n "$p" ] && ports+=("$p")
    done < <(ls -1 "$BYID" 2>/dev/null)

    i=1
    for p in "${ports[@]}"; do
        echo "  $i) $BYID/$p"
        i=$((i + 1))
    done
    echo "  m) enter a path manually"
    echo "  k) keep current  [${cur_port:-none}]"
    read -rp "Select the amplifier's serial port [k]: " sel
    case "$sel" in
        '' | k | K) new_port="$cur_port" ;;
        m | M) read -rp "  Serial port path: " new_port ;;
        *)
            if [ "$sel" -ge 1 ] 2>/dev/null && [ "$sel" -le "${#ports[@]}" ] 2>/dev/null; then
                new_port="$BYID/${ports[$((sel - 1))]}"
            else
                echo "  Invalid selection; keeping current."
                new_port="$cur_port"
            fi
            ;;
    esac
else
    # Not a Pi / no by-id aliases — accept a manual path, defaulting to current.
    echo "(No $BYID devices detected — not a Pi, or no USB serial adapter plugged in.)"
    read -rp "Serial port path [${cur_port}]: " ans
    new_port="${ans:-$cur_port}"
fi
echo "  -> serial port: ${new_port:-<unset>}"
echo

# ---------------------------------------------------------------------------
# Flex radio (optional)
# ---------------------------------------------------------------------------
cur_flex_enabled="$(cur flex.enabled)"
cur_flex_host="$(cur flex.host)"
cur_flex_port="$(cur flex.port)"; cur_flex_port="${cur_flex_port:-4992}"
cur_flex_slice="$(cur flex.slice_rx)"; cur_flex_slice="${cur_flex_slice:-0}"
cur_flex_power="$(cur flex.tune_power_watts)"; cur_flex_power="${cur_flex_power:-10}"

# Retain stored values by default even when Flex stays disabled.
new_flex_enabled="false"
new_flex_host="$cur_flex_host"
new_flex_port="$cur_flex_port"
new_flex_slice="$cur_flex_slice"
new_flex_power="$cur_flex_power"

echo "Flex radio control is OPTIONAL — it drives a FlexRadio 6000-series rig"
echo "over the SmartSDR API for orchestrated TUNE + band sweep. Skip it if you"
echo "don't have a Flex (the amplifier works fine without it)."
default_yn="N"
[ "$cur_flex_enabled" = "true" ] && default_yn="Y"
read -rp "Configure the Flex radio now? [y/N] (current: enabled=${cur_flex_enabled:-false}) " fyn
fyn="${fyn:-$default_yn}"

if [[ "$fyn" =~ ^[Yy] ]]; then
    new_flex_enabled="true"
    echo "  (Leave the IP blank to auto-discover the radio via SmartSDR UDP broadcast.)"
    read -rp "  Flex radio IP [${cur_flex_host}]: " fh
    # Empty input keeps the current value; type the word 'auto' to clear it.
    if [ -z "$fh" ]; then
        new_flex_host="$cur_flex_host"
    elif [ "$fh" = "auto" ] || [ "$fh" = "AUTO" ]; then
        new_flex_host=""
    else
        new_flex_host="$fh"
    fi
    read -rp "  SmartSDR TCP port [${cur_flex_port}]: " fp
    new_flex_port="${fp:-$cur_flex_port}"
    read -rp "  Slice to drive [${cur_flex_slice}]: " fs
    new_flex_slice="${fs:-$cur_flex_slice}"
    read -rp "  Tune carrier power, watts [${cur_flex_power}]: " fw
    new_flex_power="${fw:-$cur_flex_power}"
    echo "  -> Flex: enabled, host=${new_flex_host:-<auto-discover>} port=${new_flex_port} slice=${new_flex_slice} power=${new_flex_power}W"
else
    echo "  -> Flex: disabled (stored IP '${cur_flex_host}' retained for later)."
fi
echo

# ---------------------------------------------------------------------------
# Diff + confirm
# ---------------------------------------------------------------------------
changes=(
    "serial.port=$new_port"
    "flex.enabled=$new_flex_enabled"
    "flex.host=$new_flex_host"
    "flex.port=$new_flex_port"
    "flex.slice_rx=$new_flex_slice"
    "flex.tune_power_watts=$new_flex_power"
)

echo "=== Proposed changes to config.yaml ==="
preview="$("$PY" "$TOOL" preview "${changes[@]}")"
echo "$preview"
echo

if [ "$preview" = "(no changes)" ]; then
    echo "config.yaml already matches — nothing to do."
    exit 0
fi

read -rp "Apply these changes to config.yaml? [y/N] " ap
if [[ "$ap" =~ ^[Yy] ]]; then
    "$PY" "$TOOL" write "${changes[@]}"
    echo "config.yaml updated."
else
    echo "No changes written."
fi
