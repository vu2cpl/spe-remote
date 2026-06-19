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

Supported keys: serial.port, server.port, flex.enabled, flex.host,
flex.port, flex.slice_rx, flex.tune_power_watts.
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
    ("flex", "enabled"): _fmt_bool,
    ("flex", "host"): _fmt_qstr,
    ("flex", "port"): _fmt_int,
    ("flex", "slice_rx"): _fmt_int,
    ("flex", "tune_power_watts"): _fmt_int,
}

FLEX_DEFAULTS = {
    "enabled": "false",
    "host": '""',
    "port": "4992",
    "slice_rx": "0",
    "tune_power_watts": "10",
}


def _flex_block(values):
    """Render a fresh ``flex:`` block from ``values`` (already-formatted tokens)."""
    return (
        "# Optional FlexRadio 6000-series control for orchestrated TUNE + band\n"
        "# sweep. Leave enabled: false to run spe-remote exactly as before.\n"
        "# When enabled, spe-remote opens a second connection (SmartSDR TCP API)\n"
        "# and exposes the tune_single / tune_band / tune_stop WS commands.\n"
        "flex:\n"
        "  enabled: {enabled}\n"
        '  host: {host}   # Static LAN IP of the Flex; leave empty ("") to auto-discover\n'
        "  port: {port}              # SmartSDR TCP control port\n"
        "  slice_rx: {slice_rx}             # Which slice to drive during tune cycles\n"
        "  tune_power_watts: {tune_power_watts}    # Carrier power for ATU tunes; SPE wants 2-15W\n"
    ).format(**values)


_SECTION_RE = re.compile(r"^([A-Za-z0-9_]+):\s*(#.*)?$")


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
    return bool(re.search(r"^%s:\s*$" % re.escape(section), text, re.MULTILINE))


def _apply(text, changes):
    """Return ``text`` with all ``changes`` (dict of (section,key)->raw) applied."""
    lines = text.split("\n")
    flex_pending = {}
    for (section, key), raw in changes.items():
        fmt = SUPPORTED[(section, key)]
        formatted = fmt(raw)
        if section == "flex" and not _has_section("\n".join(lines), "flex"):
            # Collect flex keys; the block gets inserted once, below.
            flex_pending[key] = formatted
            continue
        lines, found = _set_one(lines, section, key, formatted)
        if not found and section == "flex":
            flex_pending[key] = formatted

    if flex_pending:
        values = dict(FLEX_DEFAULTS)
        values.update(flex_pending)
        block = _flex_block(values).rstrip("\n").split("\n")
        # Insert just above the logging: section if present, else append.
        insert_at = None
        for i, line in enumerate(lines):
            if re.match(r"^logging:\s*$", line):
                insert_at = i
                break
        if insert_at is None:
            if lines and lines[-1].strip() != "":
                lines.append("")
            lines.extend(block)
        else:
            lines[insert_at:insert_at] = block + [""]

    return "\n".join(lines)


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
