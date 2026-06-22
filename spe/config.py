import re
import yaml
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SerialConfig:
    port: str = "/dev/ttyUSB0"
    baudrate: int = 115200
    timeout: float = 1.0
    # Optional path to a debug raw-byte log. When set, every chunk of
    # bytes the amp sends gets appended verbatim with a monotonic
    # timestamp so we can see frame types the parser drops (anything
    # that's not CSV CNT=0x43 or RCU type=0x6A). Off by default —
    # production should not run with this on; it grows unbounded.
    # Investigation-only.
    debug_raw_log: str = ""


@dataclass
class ServerConfig:
    port: int = 8888
    host: str = "0.0.0.0"


@dataclass
class PollingConfig:
    tx_interval: float = 0.2
    idle_interval: float = 1.0
    heartbeat: float = 15.0              # force state re-broadcast every N s
    presence_heartbeat: float = 5.0      # presence/serial-status heartbeat msg every N s
    amp_alive_threshold: float = 3.0     # frames within N s ⇒ amp considered "up"


@dataclass
class FlexConfig:
    """Connection to the rig that drives the SPE.

    Phase 1 of the band-sweep work (see spe/flex.py) — the client lives
    on the Pi alongside the existing serial machinery, no integration
    with the main poll/broadcast loops yet. Set ``enabled: true`` and
    fill in ``host`` to let the upcoming tune-flow orchestrator find
    the radio. Leaving ``enabled: false`` (the default) keeps spe-remote
    behaving exactly as before.
    """
    enabled: bool = False
    host: str = ""              # Static LAN IP of the Flex; leave empty to auto-discover via SmartSDR UDP multicast (port 4992)
    port: int = 4992            # SmartSDR TCP control port
    slice_rx: int = 0           # Which slice to drive during tune cycles
    tune_power_watts: int = 10  # Carrier power for ATU tunes; SPE wants 2-15W


@dataclass
class TciConfig:
    """Connection to an ExpertSDR3 / SunSDR radio over TCI.

    TCI is the WebSocket text protocol Expert Electronics radios speak
    (default port 50001). An alternative tune backend to Flex — see
    spe/tci.py. ``trx`` selects which receiver to key.
    """
    host: str = "127.0.0.1"     # ExpertSDR3 / SunSDR TCI host
    port: int = 50001           # TCI WebSocket port
    trx: int = 0                # which TRX/receiver to drive (0 or 1)
    mode: str = "CW"            # mode set on the tuned TRX
    tune_drive: int = 0         # tune-power percent; 0 ⇒ leave to ExpertSDR


@dataclass
class RadioConfig:
    """Which tune backend is active.

    ``kind`` selects the radio family the tune orchestrator drives:
    ``"flex"`` (SmartSDR), ``"tci"`` (ExpertSDR3 / SunSDR), or ``"none"``
    (no rig — tune commands fail cleanly). Clients can change this at
    runtime over the WebSocket; it persists back to config.yaml.
    """
    kind: str = "none"          # flex | tci | none


@dataclass
class AmpConfig:
    """Amp-side characteristics that the protocol doesn't report.

    The SPE returns temperatures as unit-less integers — the user picks
    Celsius or Fahrenheit in the front-panel setup menu. This setting must
    match that choice so the web client can render the right unit symbol
    and scale gauges/thresholds correctly.
    """
    temperature_unit: str = "C"  # "C" or "F"


@dataclass
class AppConfig:
    serial: SerialConfig = field(default_factory=SerialConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    amp: AmpConfig = field(default_factory=AmpConfig)
    flex: FlexConfig = field(default_factory=FlexConfig)
    tci: TciConfig = field(default_factory=TciConfig)
    radio: RadioConfig = field(default_factory=RadioConfig)
    log_level: str = "INFO"


def persist_temperature_unit(unit: str, path: str = "config.yaml") -> bool:
    """Rewrite ``amp.temperature_unit`` in ``config.yaml`` in place.

    Uses a line-based regex substitution so comments and unrelated keys
    survive untouched (PyYAML's dump would drop all of those). If the
    ``amp:`` section doesn't exist yet, append a minimal one. Returns
    True on success.
    """
    unit = "F" if str(unit).upper().startswith("F") else "C"
    p = Path(path)
    if not p.exists():
        logger.warning(f"Cannot persist unit: {path} does not exist")
        return False

    text = p.read_text()
    pattern = re.compile(
        r"^(\s*temperature_unit\s*:\s*)([A-Za-z]+)(\s*(?:#.*)?)$",
        re.MULTILINE,
    )
    if pattern.search(text):
        new_text = pattern.sub(lambda m: f"{m.group(1)}{unit}{m.group(3)}", text)
    else:
        # Section not present — append it. Preserves the rest of the file.
        suffix = "\n\namp:\n  temperature_unit: " + unit + "\n"
        new_text = text.rstrip() + suffix

    if new_text == text:
        return True  # Already at the requested value.

    try:
        p.write_text(new_text)
        logger.info(f"Persisted temperature_unit={unit} to {path}")
        return True
    except OSError as e:
        logger.warning(f"Failed to persist temperature_unit: {e}")
        return False


def _fmt_value(value) -> str:
    """Format a Python value as the YAML scalar to write. Booleans →
    true/false, ints bare, strings quoted only when they look like a host
    (contain a dot/colon) or are empty — matching config.yaml's style
    (hosts quoted, barewords like ``flex``/``CW`` unquoted)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    s = str(value)
    if s == "" or "." in s or ":" in s:
        return '"%s"' % s
    return s


_SECTION_HDR = re.compile(r"^([A-Za-z0-9_]+):\s*(#.*)?$")
_KEY_LINE = re.compile(r"^(\s+)([A-Za-z0-9_]+)(\s*:\s*)(.*?)(\s+#.*)?\s*$")


def persist_values(changes: dict, path: str = "config.yaml") -> bool:
    """Write ``changes`` into ``config.yaml`` in place, preserving comments.

    ``changes`` maps dotted ``"section.key"`` to a value. Uses section-aware
    line substitution (the same comment-preserving approach as
    :func:`persist_temperature_unit`) so the rest of the file — including
    all explanatory comments — survives. Keys not present are inserted under
    their section (creating the section if needed). Returns True on success.
    """
    p = Path(path)
    if not p.exists():
        logger.warning(f"Cannot persist config: {path} does not exist")
        return False

    by_section: dict = {}
    for dotted, val in changes.items():
        section, key = dotted.split(".", 1)
        by_section.setdefault(section, {})[key] = val
    remaining = {(s, k) for s, kv in by_section.items() for k in kv}

    text = p.read_text()
    out, cur = [], None
    for line in text.split("\n"):
        m = _SECTION_HDR.match(line)
        if m:
            cur = m.group(1)
            out.append(line)
            continue
        if cur in by_section:
            km = _KEY_LINE.match(line)
            if km and km.group(2) in by_section[cur]:
                indent, k, sep, _old, comment = km.groups()
                out.append(f"{indent}{k}{sep}{_fmt_value(by_section[cur][k])}{comment or ''}")
                remaining.discard((cur, k))
                continue
        out.append(line)

    # Insert any keys/sections that didn't already exist.
    for section in by_section:
        missing = {k: by_section[section][k] for s, k in remaining if s == section}
        if not missing:
            continue
        block = [f"  {k}: {_fmt_value(v)}" for k, v in missing.items()]
        hdr_idx = next(
            (i for i, l in enumerate(out)
             if re.match(rf"^{re.escape(section)}:\s*(#.*)?$", l)),
            None,
        )
        if hdr_idx is not None:
            out[hdr_idx + 1:hdr_idx + 1] = block
        else:
            if out and out[-1].strip() != "":
                out.append("")
            out.append(f"{section}:")
            out.extend(block)

    new_text = "\n".join(out)
    if new_text == text:
        return True
    try:
        p.write_text(new_text)
        logger.info(f"Persisted {len(changes)} config value(s) to {path}")
        return True
    except OSError as e:
        logger.warning(f"Failed to persist config: {e}")
        return False


def load_config(path: str = "config.yaml") -> AppConfig:
    config = AppConfig()
    config_path = Path(path)

    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}

        if "serial" in raw:
            for k, v in raw["serial"].items():
                if hasattr(config.serial, k):
                    setattr(config.serial, k, v)

        if "server" in raw:
            for k, v in raw["server"].items():
                if hasattr(config.server, k):
                    setattr(config.server, k, v)

        if "polling" in raw:
            for k, v in raw["polling"].items():
                if hasattr(config.polling, k):
                    setattr(config.polling, k, v)

        if "amp" in raw:
            for k, v in raw["amp"].items():
                if hasattr(config.amp, k):
                    setattr(config.amp, k, v)
            # Normalise the unit to a single uppercase letter so downstream
            # comparisons don't have to handle "c" / "celsius" / "F" / etc.
            unit = str(config.amp.temperature_unit).strip().upper()[:1]
            config.amp.temperature_unit = "F" if unit == "F" else "C"

        if "flex" in raw:
            for k, v in raw["flex"].items():
                if hasattr(config.flex, k):
                    setattr(config.flex, k, v)

        if "tci" in raw:
            for k, v in raw["tci"].items():
                if hasattr(config.tci, k):
                    setattr(config.tci, k, v)

        if "radio" in raw and raw["radio"].get("kind"):
            config.radio.kind = str(raw["radio"]["kind"]).strip().lower()
        else:
            # Back-compat: configs written before the radio.kind selector
            # only had flex.enabled. Treat enabled Flex as kind=flex so
            # existing installs keep working untouched.
            config.radio.kind = "flex" if config.flex.enabled else "none"
        if config.radio.kind not in ("flex", "tci", "none"):
            logger.warning(f"Unknown radio.kind {config.radio.kind!r}; using 'none'")
            config.radio.kind = "none"

        if "logging" in raw:
            config.log_level = raw["logging"].get("level", "INFO")

        logger.info(f"Loaded config from {config_path}")
    else:
        logger.warning(f"Config file {config_path} not found, using defaults")

    return config
