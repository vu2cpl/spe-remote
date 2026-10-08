"""Tell the operator when a newer spe-remote release is out on GitHub.

About a minute after start, ask the GitHub releases API for the latest
spe-remote release and compare its tag with :data:`spe.__version__`. A
newer release is logged once and cached for the bundled dashboard, which
reads it from ``GET /api/update`` (see spe/app.py) and shows a small
"new version available" banner. On by default; ``updates.check: false``
in config.yaml turns it off.

Schedule (Manoj, 2026-10-09):

- Only a *successful* check — HTTP 200 and a JSON object with a usable
  ``tag_name``, newer or not — records the check time (``checked_at``)
  and sets the next check 24 h later.
- A failed check (offline, timeout, any HTTP error incl. 403 rate limit,
  bad JSON) records nothing and is retried 1 h later, so an outage at
  check time costs an hour, not a day. All of this is in memory; a
  restart checks again about a minute after start.
- A development version (``__version__`` containing "dev", any case)
  never checks automatically. The test hook below is not affected.

Rules this module keeps:

- One plain unauthenticated GET to api.github.com with the standard
  library — no token, no telemetry, no other request, no extra server.
- Runs on its own daemon thread. It never touches the tornado/asyncio
  loop, the serial port or the radio, so it cannot slow or block the
  amplifier control path; the HTTP handler only reads a cached snapshot.
- Silent on failure (offline, 403 rate limit, bad JSON): one DEBUG line.
- Never downloads or installs anything. Updating stays the operator's
  manual ``git pull`` + service restart (``UPDATE_COMMAND``).

Test hook (inert unless set): ``SPE_REMOTE_UPDATE_TEST_VERSION=0.0.1``
makes the checker compare as if that version were running, so the
banner can be seen against the real latest release.
"""

import json
import logging
import os
import re
import threading
import time
import urllib.request
from typing import Callable, Optional, Tuple

from spe import __version__

logger = logging.getLogger(__name__)

REPO = "vu2cpl/spe-remote"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_URL = "https://github.com/%s/releases" % REPO
TIMEOUT_S = 10
FIRST_CHECK_DELAY_S = 60.0          # "shortly after start"
CHECK_INTERVAL_S = 24 * 3600.0      # next check 24 h after a successful one
RETRY_AFTER_FAILURE_S = 3600.0      # ...or 1 h after a failed one
# The documented update path (README "Updating"), run in the clone.
# ./setup.sh is only needed when a release changes requirements.txt (the
# release notes say so); the banner shows the everyday command.
UPDATE_COMMAND = "git pull --ff-only && sudo systemctl restart spe-remote"
TEST_VERSION_ENV = "SPE_REMOTE_UPDATE_TEST_VERSION"


def parse_version(text) -> Tuple[int, ...]:
    """``"v3.0.1"`` -> ``(3, 0, 1)``. Leading ``v`` stripped, the rest
    split on any run of non-digits; nothing numeric gives ``()``."""
    s = str(text or "").strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    return tuple(int(part) for part in re.split(r"\D+", s) if part)


def is_newer(latest, current) -> bool:
    """True when release tag ``latest`` is a higher version than
    ``current``. Numeric tuple compare, missing components count as 0
    (so ``3.0`` == ``3.0.0`` and ``10.0`` > ``9.9``)."""
    a, b = parse_version(latest), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def is_dev_version(version) -> bool:
    """True for a development version (contains "dev", any case), e.g.
    ``3.1.0.dev0`` — those never check automatically."""
    return "dev" in str(version or "").lower()


def fetch_latest_release(timeout: float = TIMEOUT_S,
                         opener: Optional[Callable] = None) -> dict:
    """GET the latest release JSON. Raises on any network/HTTP/JSON error,
    including any status other than 200 and JSON that is not an object."""
    req = urllib.request.Request(API_URL, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "spe-remote/%s" % __version__,
    })
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        if status != 200:
            raise ValueError("HTTP %s" % status)
        data = json.loads(resp.read(1 << 20).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("release JSON is not an object")
    return data


class UpdateChecker:
    """Background release check with a thread-safe cached result."""

    def __init__(self, enabled: bool = True,
                 current_version: Optional[str] = None,
                 fetch: Callable[[], dict] = fetch_latest_release,
                 first_delay: float = FIRST_CHECK_DELAY_S,
                 interval: float = CHECK_INTERVAL_S,
                 retry_delay: float = RETRY_AFTER_FAILURE_S) -> None:
        test_version = os.environ.get(TEST_VERSION_ENV, "").strip()
        self.test_override = bool(test_version) and current_version is None
        self.current_version = current_version or test_version or __version__
        self.enabled = bool(enabled)
        # A dev version never checks by itself; the test hook still does.
        self.dev_skip = (not self.test_override
                         and is_dev_version(self.current_version))
        self._fetch = fetch
        self._first_delay = first_delay
        self._interval = interval
        self._retry_delay = retry_delay
        self._lock = threading.Lock()
        self._latest: Optional[dict] = None     # set only when newer
        self._checked_at: Optional[float] = None
        self._logged_tag: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the daemon thread (no-op when disabled or running)."""
        if not self.enabled:
            logger.info("Update check off (updates.check: false)")
            return
        if self.dev_skip:
            logger.info("Update check skipped: %s is a development version "
                        "(%s=<version> still checks)",
                        self.current_version, TEST_VERSION_ENV)
            return
        if self._thread is not None:
            return
        if self.test_override:
            logger.info("Update check: %s=%s — comparing as if that version "
                        "were running", TEST_VERSION_ENV, self.current_version)
        logger.info("Update check on: GitHub %s releases, first in %.0f s, "
                    "then %.0f h after each successful check (%.0f min "
                    "after a failed one)", REPO, self._first_delay,
                    self._interval / 3600.0, self._retry_delay / 60.0)
        self._thread = threading.Thread(
            target=self._run, name="update-check", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        # 24 h after a successful check, 1 h after a failed one. Nothing is
        # persisted: a restart starts over with the ~60 s first check.
        delay = self._first_delay
        while not self._stop.wait(delay):
            delay = self._interval if self._check() else self._retry_delay

    def check_once(self) -> bool:
        """Run one check now. Returns True when a newer release is known.
        Never raises; a failed check keeps the previous result."""
        self._check()
        with self._lock:
            return self._latest is not None

    def _check(self) -> bool:
        """One request. True on success (HTTP 200 + a usable ``tag_name``,
        newer or not) — the only case that records ``checked_at``. A
        failure records nothing and keeps the previous result."""
        try:
            data = self._fetch()
            if not isinstance(data, dict):
                raise ValueError("release JSON is not an object")
            tag = data.get("tag_name")
            if not isinstance(tag, str) or not parse_version(tag):
                raise ValueError("no usable tag_name (%r)" % (tag,))
            url = data.get("html_url")
            # Only ever link the operator to this repo's release pages.
            if not isinstance(url, str) or not url.startswith(RELEASES_URL + "/"):
                url = RELEASES_URL
        except Exception as e:  # offline, 403/rate limit, bad JSON, ...
            logger.debug("Update check failed (ignored, retry in %.0f min): %s",
                         self._retry_delay / 60.0, e)
            return False

        newer = is_newer(tag, self.current_version)
        with self._lock:
            self._checked_at = time.time()
            self._latest = {"tag": tag, "html_url": url} if newer else None
        if newer and tag != self._logged_tag:
            self._logged_tag = tag
            logger.info("spe-remote %s is available (running %s) — release "
                        "notes: %s — update with: %s",
                        tag, self.current_version, url, UPDATE_COMMAND)
        elif not newer:
            logger.debug("Update check: running %s, latest release %s",
                         self.current_version, tag)
        return True

    def status(self) -> dict:
        """Snapshot for ``GET /api/update``. Cheap; never does I/O."""
        with self._lock:
            latest = dict(self._latest) if self._latest else None
            checked_at = self._checked_at
        return {
            "enabled": self.enabled,
            "current": self.current_version,
            "update_available": latest is not None,
            "latest": latest["tag"] if latest else None,
            "html_url": latest["html_url"] if latest else None,
            "update_command": UPDATE_COMMAND,
            "checked_at": checked_at,
        }
