"""Tests for configtool.py's write path on an older config.yaml.

The Pi upgrade path is the case that matters: a config.yaml written
before the multi-radio work has no `radio:` or `tci:` block at all, and
a write to one used to be dropped silently — spe-remote then started on
the wrong backend with no diagnostic. Pure text in, text out; no files,
no PyYAML (which configtool only needs for `get`).
"""
import sys

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from configtool import _apply  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    if cond:
        print(f"[PASS] {label}")
    else:
        FAILURES.append(label)
        print(f"[FAIL] {label} {detail}")


# A config.yaml from before the radio work: no radio:, no tci:, and a
# flex: block that predates slice_rx.
PRE_RADIO = """\
serial:
  # Which USB adapter the amp is on.
  port: /dev/ttyUSB0
  baudrate: 115200

server:
  port: 8888
  host: "0.0.0.0"

# Optional FlexRadio control.
flex:
  enabled: false
  host: ""
  port: 4992

logging:
  level: INFO
"""


def keys_of(text, section):
    """Return {key: value} for one flat section of the rendered text."""
    out, cur = {}, None
    for line in text.split("\n"):
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            cur = line.rstrip()[:-1]
            continue
        if line[:1] == "#" or not line.strip():
            continue
        if cur == section and line.startswith("  ") and ":" in line:
            k, v = line.strip().split(":", 1)
            out[k] = v.split("#")[0].strip()
    return out


def t1_missing_sections_are_created():
    """The exact case from review: `write radio.kind=tci tci.host=...`
    against a pre-radio config used to drop both writes."""
    new = _apply(PRE_RADIO, {("radio", "kind"): "tci",
                             ("tci", "host"): "192.168.1.10"})
    check("t1 radio section created", keys_of(new, "radio") == {"kind": "tci"},
          str(keys_of(new, "radio")))
    tci = keys_of(new, "tci")
    check("t1 tci.host written", tci.get("host") == '"192.168.1.10"', str(tci))
    check("t1 tci defaults filled in",
          (tci.get("port"), tci.get("trx"), tci.get("mode"),
           tci.get("tune_drive")) == ("50001", "0", "CW", "0"), str(tci))
    check("t1 existing comments survive",
          "# Which USB adapter the amp is on." in new
          and "# Optional FlexRadio control." in new)
    check("t1 untouched keys survive",
          keys_of(new, "serial") == {"port": "/dev/ttyUSB0",
                                     "baudrate": "115200"},
          str(keys_of(new, "serial")))


def t2_missing_key_in_existing_section():
    """A section that exists but lacks the key (an old flex: block with
    no slice_rx:) gets the key added, not dropped."""
    new = _apply(PRE_RADIO, {("flex", "slice_rx"): "2"})
    flex = keys_of(new, "flex")
    check("t2 slice_rx added", flex.get("slice_rx") == "2", str(flex))
    check("t2 existing flex keys kept",
          (flex.get("enabled"), flex.get("port")) == ("false", "4992"),
          str(flex))
    check("t2 added below the section's own keys",
          new.index("slice_rx") > new.index("port: 4992"))


def t3_existing_keys_still_substituted():
    """The original behaviour — substitute in place, keep the inline
    comment — is unchanged."""
    src = PRE_RADIO.replace('  host: ""', '  host: ""   # leave empty to discover')
    new = _apply(src, {("flex", "host"): "192.168.1.148",
                       ("flex", "enabled"): "yes"})
    flex = keys_of(new, "flex")
    check("t3 host substituted", flex.get("host") == '"192.168.1.148"', str(flex))
    check("t3 enabled coerced to a bool token",
          flex.get("enabled") == "true", str(flex))
    check("t3 inline comment preserved",
          "# leave empty to discover" in new)
    check("t3 no duplicate host key", new.count("  host:") == 2)  # flex + server


def t4_new_sections_land_above_logging():
    new = _apply(PRE_RADIO, {("tci", "host"): "127.0.0.1"})
    check("t4 tci: before logging:",
          new.index("\ntci:") < new.index("\nlogging:"), new)
    check("t4 logging block intact", "logging:\n  level: INFO" in new)


def main():
    for t in (t1_missing_sections_are_created, t2_missing_key_in_existing_section,
              t3_existing_keys_still_substituted, t4_new_sections_land_above_logging):
        t()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
