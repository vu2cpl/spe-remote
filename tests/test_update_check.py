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

import io  # noqa: E402
import json  # noqa: E402
import urllib.error  # noqa: E402

import spe.update_check as update_check  # noqa: E402
from spe import __version__  # noqa: E402
from spe.update_check import (  # noqa: E402
    API_URL, CHECK_INTERVAL_S, RELEASES_URL, RETRY_AFTER_FAILURE_S,
    UPDATE_COMMAND, UpdateChecker, fetch_latest_release, is_dev_version,
    is_newer, parse_version,
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
check("status carries update command",
      st["update_command"] == UPDATE_COMMAND
      == "git pull --ff-only && sudo systemctl restart spe-remote",
      str(st["update_command"]))
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


# --- checked_at: only a successful check records it --------------------
def flaky(*outcomes):
    """fetch() that plays back outcomes in order: a dict is returned, an
    exception is raised."""
    queue = list(outcomes)

    def fetch():
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
    return fetch


GOOD_NEWER = {"tag_name": "v3.1.0", "html_url": RELEASES_URL + "/tag/v3.1.0"}
GOOD_SAME = {"tag_name": "v3.0.0", "html_url": RELEASES_URL + "/tag/v3.0.0"}
HTTP_403 = urllib.error.HTTPError(API_URL, 403, "rate limit exceeded", {}, None)
FAILURES_KINDS = [
    OSError("network is unreachable"),
    TimeoutError("timed out"),
    HTTP_403,
    urllib.error.HTTPError(API_URL, 500, "server error", {}, None),
    json.JSONDecodeError("Expecting value", "<html>", 0),
    ["not", "a", "dict"],
    {"name": "no tag_name here"},
]

for good, label in ((GOOD_NEWER, "newer"), (GOOD_SAME, "not newer")):
    uc = UpdateChecker(current_version="3.0.0", fetch=flaky(good))
    assert uc.status()["checked_at"] is None
    ok = uc._check()
    check(f"success ({label}) records checked_at",
          ok is True and isinstance(uc.status()["checked_at"], float))

for bad in FAILURES_KINDS:
    uc = UpdateChecker(current_version="3.0.0", fetch=flaky(bad))
    kind = " ".join(str(x) for x in (type(bad).__name__,
                                     getattr(bad, "code", "")) if x)
    check(f"failure ({kind}) records nothing",
          uc._check() is False and uc.status()["checked_at"] is None
          and uc.status()["update_available"] is False)

uc = UpdateChecker(current_version="3.0.0",
                   fetch=flaky(GOOD_NEWER, HTTP_403, OSError("offline")))
uc.check_once()
first = uc.status()["checked_at"]
uc._check()
uc.check_once()
check("failures after a success keep its time and result",
      uc.status()["checked_at"] == first
      and uc.status()["latest"] == "v3.1.0")


# --- fetch_latest_release: HTTP 200 + JSON object only ------------------
class FakeResp(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status


def opener_for(body, status=200):
    def opener(req, timeout):
        assert req.full_url == API_URL and timeout == update_check.TIMEOUT_S
        return FakeResp(body, status)
    return opener


check("fetch: 200 + JSON object returned",
      fetch_latest_release(opener=opener_for(b'{"tag_name": "v3.1.0"}'))
      == {"tag_name": "v3.1.0"})
for body, status, label in ((b'{"tag_name": "v3.1.0"}', 203, "non-200 status"),
                            (b"[1, 2]", 200, "JSON array"),
                            (b"<html>", 200, "not JSON")):
    try:
        fetch_latest_release(opener=opener_for(body, status))
        check(f"fetch: {label} raises", False)
    except Exception:
        check(f"fetch: {label} raises", True)


def http_403(req, timeout):
    raise HTTP_403


try:
    fetch_latest_release(opener=http_403)
    check("fetch: 403 raises", False)
except urllib.error.HTTPError:
    check("fetch: 403 raises", True)


# --- schedule: 24 h after a success, 1 h after a failure ----------------
class FakeStop:
    """Stands in for the stop Event: records each wait, ends the loop
    after ``rounds`` checks."""

    def __init__(self, rounds):
        self.rounds = rounds
        self.waits = []

    def wait(self, delay):
        self.waits.append(delay)
        return len(self.waits) > self.rounds

    def set(self):
        pass


uc = UpdateChecker(current_version="3.0.0", first_delay=60.0,
                   fetch=flaky(OSError("offline"), HTTP_403, GOOD_SAME,
                               TimeoutError("timed out"), GOOD_NEWER))
uc._stop = FakeStop(rounds=5)
uc._run()
H, D = RETRY_AFTER_FAILURE_S, CHECK_INTERVAL_S
check("defaults: 1 h retry, 24 h interval", H == 3600.0 and D == 86400.0)
check("schedule: first 60 s, 1 h after each failure, 24 h after success",
      uc._stop.waits == [60.0, H, H, D, H, D], str(uc._stop.waits))
check("schedule: banner from the last successful check",
      uc.status()["latest"] == "v3.1.0")


# --- development versions never check automatically ---------------------
check("dev detection, any case",
      is_dev_version("3.1.0.dev0") and is_dev_version("3.1.0-DEV")
      and is_dev_version("Dev") and not is_dev_version("3.0.0")
      and not is_dev_version(None))
check("released version is not a dev version",
      not is_dev_version(__version__), __version__)

calls = []
uc = UpdateChecker(current_version="3.0.1.dev0",
                   fetch=lambda: calls.append(1) or GOOD_NEWER)
uc.start()
check("dev version: no automatic check thread",
      uc.dev_skip is True and uc._thread is None and calls == [])
check("dev version: skip logged at INFO",
      any("development version" in r.getMessage() for r in info_lines()))
check("dev version: a manual check still works", uc.check_once() is True)

real_version = update_check.__version__
update_check.__version__ = "3.1.0.DEV1"
try:
    uc = UpdateChecker(fetch=release("v3.0.0"))
    check("dev __version__ picked up without the hook",
          uc.current_version == "3.1.0.DEV1" and uc.dev_skip is True)
    os.environ["SPE_REMOTE_UPDATE_TEST_VERSION"] = "0.0.1"
    uc = UpdateChecker(fetch=release("v3.0.0"), first_delay=3600.0)
    uc.start()
    started = uc._thread is not None
    uc.stop()
    if started:
        uc._thread.join(2)
    check("test hook unaffected by a dev __version__",
          uc.dev_skip is False and started and uc.check_once() is True)
finally:
    os.environ.pop("SPE_REMOTE_UPDATE_TEST_VERSION", None)
    update_check.__version__ = real_version

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("ALL PASS")
