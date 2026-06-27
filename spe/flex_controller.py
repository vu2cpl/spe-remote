"""On-demand lifecycle manager for the Flex radio connection.

The SmartSDR TCP session is only needed while the operator is actually
running an ATU tune cycle / band sweep. Holding it open for the whole
life of the server keeps the radio marked "in use" by another client
and means spe-remote must be restarted if the radio is power-cycled.

``FlexController`` makes the connection on-demand instead:

  * ``connect()`` is called when the operator opens the Sweep menu
    (the ``flex_connect`` WS command) and, as a safety net, lazily at
    the start of every tune cycle — so older clients that don't send
    ``flex_connect`` still work.
  * ``disconnect()`` is called when the tune cycle/sweep finishes (the
    orchestrator's ``finally``) and when the Sweep menu is closed while
    idle (the ``flex_disconnect`` WS command).

Host resolution (static ``flex.host`` vs. UDP discovery) is deferred to
``connect()`` too, so the radio may be powered off when spe-remote
starts and still be found later when the menu is opened.

All transitions are serialised with an ``asyncio.Lock`` and surfaced to
WS clients through the same ``tune_event`` channel the orchestrator
uses, with these phases:

    FLEX_CONNECTING  FLEX_CONNECTED  FLEX_DISCONNECTED  FLEX_ERROR
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from spe.config import FlexConfig
from spe.flex import FlexConnection, discover as flex_discover

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str, str], None]


class FlexController:
    """Owns at most one :class:`FlexConnection`, opened/closed on demand."""

    def __init__(
        self,
        config: FlexConfig,
        on_status: Optional[StatusCallback] = None,
    ):
        self.config = config
        self.on_status = on_status

        self._conn: Optional[FlexConnection] = None
        self._lock = asyncio.Lock()
        # Cache a discovered host so we don't re-run the 5 s UDP discovery
        # on every connect once we've found the radio once.
        self._resolved_host: str = config.host or ""

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def connection(self) -> Optional[FlexConnection]:
        """The live connection, or None when disconnected."""
        return self._conn if (self._conn and self._conn.is_connected) else None

    @property
    def is_connected(self) -> bool:
        return self._conn is not None and self._conn.is_connected

    def _status(self, phase: str, message: str = "") -> None:
        logger.info(f"Flex[{phase}] {message}".rstrip())
        cb = self.on_status
        if cb is not None:
            try:
                cb(phase, message)
            except Exception:
                logger.exception("Flex on_status callback raised")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> Optional[FlexConnection]:
        """Ensure a live connection and return it (or None on failure).

        Idempotent: a no-op that returns the existing connection when one
        is already up. Resolves the host (static or via discovery) on the
        first call. Never raises — failures are reported via FLEX_ERROR
        and surfaced as a None return so callers can decide what to do.
        """
        async with self._lock:
            if self.is_connected:
                return self._conn

            host = self._resolved_host
            if not host:
                self._status(
                    "FLEX_CONNECTING",
                    "flex.host empty — discovering radio on UDP 4992 (up to 5s)…",
                )
                try:
                    discovery = await flex_discover()
                except Exception as e:
                    logger.exception("Flex discovery raised")
                    self._status("FLEX_ERROR", f"discovery failed: {e}")
                    return None
                if discovery and discovery.get("ip"):
                    host = discovery["ip"]
                    self._resolved_host = host
                    logger.info(
                        f"Flex: discovered {discovery.get('model', '?')} "
                        f"\"{discovery.get('nickname', '?')}\" "
                        f"({discovery.get('callsign', '?')}) at {host}"
                    )
                else:
                    self._status(
                        "FLEX_ERROR",
                        "no radio answered discovery (is it powered on?)",
                    )
                    return None

            self._status("FLEX_CONNECTING", f"{host}:{self.config.port}")
            conn = FlexConnection(host, self.config.port)
            try:
                await conn.connect()
            except Exception as e:
                logger.exception("Flex connect failed")
                # Make sure we don't leak a half-open socket.
                try:
                    await conn.close()
                except Exception:
                    pass
                self._status("FLEX_ERROR", f"connect to {host} failed: {e}")
                return None

            self._conn = conn
            self._status(
                "FLEX_CONNECTED",
                f"{host} (version={conn.radio_version or '?'})",
            )
            return conn

    async def disconnect(self) -> None:
        """Close the connection if open. Idempotent; never raises."""
        async with self._lock:
            if self._conn is None:
                return
            try:
                await self._conn.close()
            except Exception:
                logger.exception("Error closing Flex connection")
            finally:
                self._conn = None
                self._status("FLEX_DISCONNECTED")
