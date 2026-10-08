"""Tornado application setup with static file serving."""

import os
import logging

import tornado.web

from spe.websocket_handler import AmplifierWebSocket

logger = logging.getLogger(__name__)

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


class NoCacheStaticFileHandler(tornado.web.StaticFileHandler):
    """StaticFileHandler that always asks the browser to revalidate.

    We don't have a build pipeline that fingerprints filenames, so without
    this the browser happily serves stale index.html / app.js / style.css
    after a ``git pull``. ``no-cache`` does NOT mean "do not cache" — it
    means "cache, but always revalidate before using". Combined with
    Tornado's automatic ETag / Last-Modified handling, unchanged assets
    come back as cheap 304 Not Modified responses; changed ones are
    re-fetched. Net effect: users always get the latest UI without a
    manual hard-reload, with negligible bandwidth overhead on a LAN.
    """

    def set_extra_headers(self, path: str) -> None:
        self.set_header("Cache-Control", "no-cache, must-revalidate")


class UpdateStatusHandler(tornado.web.RequestHandler):
    """``GET /api/update`` — the cached result of the GitHub release check
    (spe/update_check.py), read by the dashboard's update banner.

    Only reads a snapshot; the check itself runs on its own thread, so a
    slow or offline GitHub can never hold up this request or the WS."""

    def initialize(self, checker=None) -> None:
        self._checker = checker

    def get(self) -> None:
        self.set_header("Cache-Control", "no-store")
        if self._checker is None:
            self.write({"enabled": False, "update_available": False})
        else:
            self.write(self._checker.status())


def make_app(update_checker=None) -> tornado.web.Application:
    return tornado.web.Application([
        (r"/ws", AmplifierWebSocket),
        (r"/api/update", UpdateStatusHandler, {"checker": update_checker}),
        (r"/(.*)", NoCacheStaticFileHandler, {
            "path": WEB_DIR,
            "default_filename": "index.html",
        }),
    ])
