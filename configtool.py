#!/usr/bin/env python3
"""Comment-preserving editor for spe-remote's ``config.yaml``.

The install/update scripts use this to set the handful of host-specific
keys — the serial port and the optional Flex radio block — without
disturbing the surrounding comments. A naive PyYAML round-trip would
drop every comment in the file, so writes are done with targeted,
section-aware line substitutions (the same approach as
``spe.config.persist_temperature_unit``). Reads use PyYAML because
accuracy matters more than formatting there.

Usage:
  configtool.py get   <section.key>                 # print current value
  configtool.py preview <section.key=value> ...     # show a unified diff, no write
  configtool.py write   <section.key=value> ...     # apply the changes

Supported keys:
  serial.port, server.port
  radio.kind                                      # flex | tci | none
  flex.enabled, flex.host, flex.port, flex.slice_rx, flex.tune_power_watts
  tci.host, tci.port, tci.trx, tci.mode, tci.tune_drive

A key whose section (or whose section's key) is missing from an older
config.yaml is added rather than dropped — that's the Pi upgrade path,
where a pre-radio config has no radio:/tci: block at all.
"""
import sys
import re
import difflib
from pathlib import Path

# Note: ``import yaml`` is deliberately lazy (see _load_raw). Only the ``get``
# command needs PyYAML; ``preview`` and ``write`` are pure regex + difflib, so
# they keep working even on a stripped-down Python without PyYAML installed.

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


# --- value formatters: turn a raw string into the YAML token we write ---
def _fmt_bool(v):
    return "true" if str(v).strip().lower() in ("1", "true", "yes", "y", "on") else "false"


def _fmt_int(v):
    return str(int(str(v).strip()))


def _fmt_plain(v):
    return str(v).strip()


def _fmt_qstr(v):
    # Quote the value the way the rest of config.yaml does (host: "1.2.3.4").
    return '"%s"' % str(v).strip().strip('"').strip("'")


# (section, key) -> formatter. The order here is also the order the flex
# block is rendered in when it has to be synthesised from scratch.
SUPPORTED = {
    ("serial", "port"): _fmt_plain,
    ("server", "port"): _fmt_int,
    ("radio", "kind"): _fmt_plain,
    ("flex", "enabled"): _fmt_bool,
    ("flex", "host"): _fmt_qstr,
    ("flex", "port"): _fmt_int,
    ("flex", "slice_rx"): _fmt_int,
    ("flex", "tune_power_watts"): _fmt_int,
    ("tci", "host"): _fmt_qstr,
    ("tci", "port"): _fmt_int,
    ("tci", "trx"): _fmt_int,
    ("tci", "mode"): _fmt_plain,
    ("tci", "tune_drive"): _fmt_int,
}

# Sections this tool can synthesise from scratch when config.yaml
# predates them — the Pi upgrade path, where an older file has no
# radio:/tci: block at all and a write to one would otherwise be dropped
# silently (leaving spe-remote on the wrong backend, with no diagnostic).
# Each entry is the block's leading comment plus its keys in render
# order, with a default token and an inline comment; keys being written
# override the defaults.
SECTION_TEMPLATES = {
    "radio": (
        "# Which rig spe-remote drives for orchestrated TUNE / band sweep.\n"
        "# Clients can also change this at runtime over the WebSocket.\n",
        [
            ("kind", "none", "   # flex | tci | none"),
        ],
    ),
    "flex": (
        "# Optional FlexRadio 6000-series control for orchestrated TUNE + band\n"
        "# sweep. Leave enabled: false to run spe-remote exactly as before.\n"
        "# When enabled, spe-remote opens a second connection (SmartSDR TCP API)\n"
        "# and exposes the tune_single / tune_band / tune_stop WS commands.\n",
        [
            ("enabled", "false", ""),
            ("host", '""', '   # Static LAN IP of the Flex; leave empty ("") to auto-discover'),
            ("port", "4992", "              # SmartSDR TCP control port"),
            ("slice_rx", "0", "             # Which slice to drive during tune cycles"),
            ("tune_power_watts", "10", "    # Carrier power for ATU tunes; SPE wants 2-15W"),
        ],
    ),
    "tci": (
        "# Expert Electronics SunSDR / ExpertSDR3 control over TCI — the other\n"
        "# tune backend. Used when radio.kind is tci; the WebSocket protocol\n"
        "# ExpertSDR3 speaks, default port 50001.\n",
        [
            ("host", '"127.0.0.1"', "   # ExpertSDR3 / SunSDR TCI host"),
            ("port", "50001", "        # TCI WebSocket port"),
            ("trx", "0", "             # Which TRX/receiver to drive (0 or 1)"),
            ("mode", "CW", "            # Mode set on the tuned TRX"),
            ("tune_drive", "0", "      # Tune-power percent; 0 = leave it to ExpertSDR"),
        ],
    ),
}


def _render_section(section, values):
    """Render a fresh ``section:`` block as a list of lines. ``values``
    maps key -> already-formatted token and overrides the defaults. A
    section with no template gets a bare header plus the keys written."""
    header, keys = SECTION_TEMPLATES.get(section, ("", []))
    lines = header.rstrip("\n").split("\n") if header else []
    lines.append("%s:" % section)
    written = set()
    for key, default, comment in keys:
        lines.append("  %s: %s%s" % (key, values.get(key, default), comment))
        written.add(key)
    for key, formatted in values.items():
        if key not in written:
            lines.append("  %s: %s" % (key, formatted))
    return lines


_SECTION_RE = re.compile(r"^([A-Za-z0-9_]+):\s*(#.*)?$")
_ANY_KEY_RE = re.compile(r"^(\s+)([A-Za-z0-9_]+)(\s*:\s*)(.*?)(\s+#.*)?\s*$")


def _set_one(lines, section, key, formatted):
    """Substitute ``section.key``'s value in ``lines`` in place.

    Returns (new_lines, found). Preserves indentation and any inline
    comment. Only the first matching key inside the target section is
    touched.
    """
    key_re = re.compile(r"^(\s+)(%s)(\s*:\s*)(.*?)(\s+#.*)?\s*$" % re.escape(key))
    out, cur, done = [], None, False
    for line in lines:
        m = _SECTION_RE.match(line)
        if m:
            cur = m.group(1)
            out.append(line)
            continue
        if cur == section and not done:
            km = key_re.match(line)
            if km:
                indent, k, sep, _old, comment = km.groups()
                out.append("%s%s%s%s%s" % (indent, k, sep, formatted, comment or ""))
                done = True
                continue
        out.append(line)
    return out, done


def _has_section(text, section):
    return bool(re.search(r"^%s:\s*(#.*)?$" % re.escape(section),
                          text, re.MULTILINE))


def _insert_key(lines, section, key, formatted):
    """Add ``key`` to an existing ``section`` that doesn't have it yet
    (e.g. a tci: block written by an older build with no tune_drive:).

    Goes after the section's *last* existing key, not straight after the
    header — the header's explanatory comments sit in between, and new
    keys belong below them."""
    at, cur = None, None
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if m:
            if cur == section:
                break              # next section: stop at what we had
            cur = m.group(1)
            if cur == section:
                at = i + 1
            continue
        if cur == section and _ANY_KEY_RE.match(line):
            at = i + 1
    if at is None:                 # section vanished between checks
        return lines
    return lines[:at] + ["  %s: %s" % (key, formatted)] + lines[at:]


def _apply(text, changes):
    """Return ``text`` with all ``changes`` (dict of (section,key)->raw) applied."""
    lines = text.split("\n")
    pending = {}
    for (section, key), raw in changes.items():
        formatted = SUPPORTED[(section, key)](raw)
        if not _has_section("\n".join(lines), section):
            # Whole section missing: collect its keys, synthesise once below.
            pending.setdefault(section, {})[key] = formatted
            continue
        lines, found = _set_one(lines, section, key, formatted)
        if not found:
            lines = _insert_key(lines, section, key, formatted)

    # Templated sections first, in template order, so a config gaining
    # both radio: and tci: gets them in the documented order.
    ordered = ([s for s in SECTION_TEMPLATES if s in pending]
               + [s for s in pending if s not in SECTION_TEMPLATES])
    for section in ordered:
        lines = _insert_block(lines, _render_section(section, pending[section]))

    return "\n".join(lines)


def _insert_block(lines, block):
    """Put a synthesised section just above ``logging:`` (the file ends
    with it, and radio blocks read better next to the other settings),
    or append it."""
    insert_at = None
    for i, line in enumerate(lines):
        if re.match(r"^logging:\s*(#.*)?$", line):
            insert_at = i
            break
    if insert_at is None:
        if lines and lines[-1].strip() != "":
            lines.append("")
        return lines + block
    return lines[:insert_at] + block + [""] + lines[insert_at:]


def _parse_changes(args):
    changes = {}
    for a in args:
        if "=" not in a:
            sys.exit("error: expected section.key=value, got %r" % a)
        dotted, value = a.split("=", 1)
        parts = dotted.split(".")
        if len(parts) != 2 or (parts[0], parts[1]) not in SUPPORTED:
            sys.exit("error: unsupported key %r" % dotted)
        changes[(parts[0], parts[1])] = value
    return changes


def _load_raw():
    import yaml  # lazy: only the `get` path needs PyYAML

    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def cmd_get(args):
    if len(args) != 1 or "." not in args[0]:
        sys.exit("usage: configtool.py get <section.key>")
    section, key = args[0].split(".", 1)
    raw = _load_raw()
    val = raw.get(section, {})
    if isinstance(val, dict):
        val = val.get(key)
    else:
        val = None
    if val is None:
        return
    if isinstance(val, bool):
        print("true" if val else "false")
    else:
        print(val)


def cmd_preview(args):
    changes = _parse_changes(args)
    old = CONFIG_PATH.read_text() if CONFIG_PATH.exists() else ""
    new = _apply(old, changes)
    if old == new:
        print("(no changes)")
        return
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile="config.yaml (current)",
        tofile="config.yaml (new)",
    )
    sys.stdout.writelines(diff)


def cmd_write(args):
    changes = _parse_changes(args)
    old = CONFIG_PATH.read_text() if CONFIG_PATH.exists() else ""
    new = _apply(old, changes)
    if old == new:
        return
    CONFIG_PATH.write_text(new)


def main(argv):
    if not argv:
        sys.exit(__doc__)
    cmd, rest = argv[0], argv[1:]
    if cmd == "get":
        cmd_get(rest)
    elif cmd == "preview":
        cmd_preview(rest)
    elif cmd == "write":
        cmd_write(rest)
    else:
        sys.exit("unknown command %r (use get/preview/write)" % cmd)


if __name__ == "__main__":
    main(sys.argv[1:])
