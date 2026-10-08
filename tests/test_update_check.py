"""Tests for the GitHub release update check (spe/update_check.py).

The version comparison is a pure function; the checker is driven with a
fake fetch, so nothing here touches the network, tornado or pyyaml.
Run with: python3 tests/test_update_check.py
"""
import logging
import os
import sys

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The test hook must not leak in from the shell running the tests.
os.environ.pop("SPE_REMOTE_UPDATE_TEST_VERSION", None)

from spe import __version__  # noqa: E402
from spe.update_check import (  # noqa: E402
    RELEASES_URL, UpdateChecker, is_newer, parse_version,
)

FAILURES = []


def check(label, cond, detail=""):
    if cond:
        print(f"[PASS] {label}")
    else:
        FAILURES.append(label)
        print(f"[FAIL] {label} {detail}")


# --- parse_version / is_newer (pure) ------------------------------------
check("parse v3.0.0", parse_version("v3.0.0") == (3, 0, 0))
check("parse V2.76.7-mac12", parse_version("V2.76.7-mac12") == (2, 76, 7, 12))
check("parse garbage -> ()", parse_version("latest") == ())
check("parse None -> ()", parse_version(None) == ())
check("same version, v prefix ignored", not is_newer("v3.0.0", "3.0.0"))
check("patch bump is newer", is_newer("v3.0.1", "3.0.0"))
check("minor bump beats higher patch", is_newer("v3.1", "3.0.9"))
check("numeric, not lexical", is_newer("v10.0.0", "9.9.9"))
check("missing parts count as 0", not is_newer("v3.0", "3.0.0")
      and not is_newer("3.0.0", "v3"))
check("extra non-zero part is newer", is_newer("3.0.0.1", "3.0.0"))
check("older release is not newer", not is_newer("v2.1.0", "3.0.0"))
check("package version parses", len(parse_version(__version__)) >= 2,
      repr(__version__))


# --- UpdateChecker with a fake fetch -----------------------------------
class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


cap = Capture()
log = logging.getLogger("spe.update_check")
log.addHandler(cap)
log.setLevel(logging.DEBUG)


def info_lines():
    return [r for r in cap.records if r.levelno >= logging.INFO]


def release(tag, url=RELEASES_URL + "/tag/v9.9.9"):
    return lambda: {"tag_name": tag, "html_url": url}


uc = UpdateChecker(current_version="3.0.0", fetch=release("v3.1.0"))
check("before any check: no update", uc.status()["update_available"] is False)
check("newer release found", uc.check_once() is True)
st = uc.status()
check("status carries tag + link", st["latest"] == "v3.1.0"
      and st["html_url"].startswith(RELEASES_URL), str(st))
check("status carries update command", "git pull" in st["update_command"])
uc.check_once()
check("logged once, not on every check",
      len([r for r in info_lines() if "is available" in r.getMessage()]) == 1)

cap.records.clear()
uc._fetch = release("v3.0.0")
check("same version -> no update", uc.check_once() is False
      and uc.status()["update_available"] is False)
check("same version logs nothing at INFO", info_lines() == [])

cap.records.clear()
uc = UpdateChecker(current_version="3.0.0", fetch=release("v3.1.0"))
uc.check_once()


def offline():
    raise OSError("network is unreachable")


uc._fetch = offline
check("failure keeps previous result", uc.check_once() is True
      and uc.status()["latest"] == "v3.1.0")
uc._fetch = lambda: ["not", "a", "dict"]
check("bad JSON shape is silent", uc.check_once() is True)
uc._fetch = release(None)
check("missing tag_name is silent", uc.check_once() is True)
failed = [r for r in cap.records if "failed" in r.getMessage()]
check("failures only at DEBUG", len(failed) == 3
      and all(r.levelno == logging.DEBUG for r in failed),
      str([(r.levelname, r.getMessage()) for r in failed]))

uc = UpdateChecker(current_version="3.0.0",
                   fetch=release("v3.1.0", "https://evil.example/x"))
uc.check_once()
check("foreign html_url replaced by the releases page",
      uc.status()["html_url"] == RELEASES_URL)

uc = UpdateChecker(enabled=False, fetch=release("v9.0.0"))
uc.start()
check("disabled: no thread started", uc._thread is None
      and uc.status()["enabled"] is False)

os.environ["SPE_REMOTE_UPDATE_TEST_VERSION"] = "0.0.1"
uc = UpdateChecker(fetch=release("v3.0.0"))
check("test hook overrides the running version",
      uc.current_version == "0.0.1" and uc.check_once() is True)
os.environ.pop("SPE_REMOTE_UPDATE_TEST_VERSION")
uc = UpdateChecker(fetch=release(__version__))
check("hook unset: real version used, no update",
      uc.current_version == __version__ and uc.check_once() is False)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("ALL PASS")
