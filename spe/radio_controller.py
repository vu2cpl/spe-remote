"""On-demand lifecycle manager for the tune radio (any backend).

Generalises the former FlexController: the SmartSDR/TCI control session is
only needed while the operator is actually running a tune cycle / sweep,
so it is opened on demand (Sweep-menu open, or lazily at tune start) and
closed when the cycle is over. This keeps the radio free for other clients
and lets it be powered off until needed.

The backend is chosen by ``radio.kind``:

  * ``flex`` → :class:`spe.flex.FlexConnection` (SmartSDR TCP, host may be
    discovered via UDP when ``flex.host`` is empty),
  * ``tci``  → :class:`spe.tci.TciConnection` (ExpertSDR3 / SunSDR),
  * ``none`` → no radio; tune commands fail cleanly.

``reconfigure()`` swaps the active kind/settings in place so a client can
switch radios at runtime without restarting the server — the orchestrator
holds one stable reference to this controller across the switch.

Transitions broadcast on the same ``tune_event`` channel the orchestrator
uses, with these phases:

    RADIO_CONNECTING  RADIO_CONNECTED  RADIO_DISCONNECTED  RADIO_ERROR
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from spe.config import RadioConfig, FlexConfig, TciConfig
from spe.radio import RadioConnection
from spe.flex import FlexConnection, discover as flex_discover
from spe.tci import TciConnection

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str, str], None]


class RadioController:
    """Owns at most one :class:`RadioConnection`, opened/closed on demand."""

    def __init__(
        self,
        radio: RadioConfig,
        flex: FlexConfig,
        tci: TciConfig,
        on_status: Optional[StatusCallback] = None,
    ):
        self.radio = radio
        self.flex = flex
        self.tci = tci
        self.on_status = on_status

        self._conn: Optional[RadioConnection] = None
        self._lock = asyncio.Lock()
        # Cache a discovered Flex host so we don't re-run UDP discovery.
        self._resolved_flex_host: str = flex.host or ""

    # ------------------------------------------------------------------
    # State / accessors the orchestrator reads
    # ------------------------------------------------------------------

    @property
    def kind(self) -> str:
        return self.radio.kind

    @property
    def channel(self) -> int:
        """Backend channel to drive: Flex slice or TCI trx."""
        if self.kind == "tci":
            return self.tci.trx
        return self.flex.slice_rx

    @property
    def tune_power_watts(self) -> int:
        """Power hint for set_tune_power (Flex watts; TCI ignores it and
        uses its own configured tune_drive)."""
        return self.flex.tune_power_watts if self.kind == "flex" else 0

    @property
    def connection(self) -> Optional[RadioConnection]:
        return self._conn if (self._conn and self._conn.is_connected) else None

    @property
    def is_connected(self) -> bool:
        return self._conn is not None and self._conn.is_connected

    def _status(self, phase: str, message: str = "") -> None:
        logger.info("Radio[%s] %s", phase, message)
        cb = self.on_status
        if cb is not None:
            try:
                cb(phase, message)
            except Exception:
                logger.exception("Radio on_status callback raised")

    # ------------------------------------------------------------------
    # Live reconfiguration
    # ------------------------------------------------------------------

    def reconfigure(self, radio: RadioConfig, flex: FlexConfig,
                    tci: TciConfig) -> None:
        """Swap the active radio kind/settings. Caller must disconnect()
        first; the next connect() builds the new backend."""
        self.radio = radio
        self.flex = flex
        self.tci = tci
        self._resolved_flex_host = flex.host or ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> Optional[RadioConnection]:
        """Ensure a live connection and return it (or None on failure).

        Idempotent. Never raises — failures report RADIO_ERROR and return
        None so the orchestrator can FAIL the cycle cleanly."""
        async with self._lock:
            if self.is_connected:
                return self._conn

            kind = self.kind
            if kind == "none":
                self._status("RADIO_ERROR", "no radio configured")
                return None

            conn = await self._build(kind)
            if conn is None:
                return None

            self._status("RADIO_CONNECTING", self._target_desc(kind))
            try:
                await conn.connect()
            except Exception as e:
                logger.exception("Radio connect failed")
                try:
                    await conn.close()
                except Exception:
                    pass
                self._status("RADIO_ERROR", f"connect failed: {e}")
                return None

            self._conn = conn
            self._status(
                "RADIO_CONNECTED",
                f"{kind}: {self._target_desc(kind)} "
                f"(version={conn.radio_version or '?'})",
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
                logger.exception("Error closing radio connection")
            finally:
                self._conn = None
                self._status("RADIO_DISCONNECTED")

    # ------------------------------------------------------------------
    # Backend construction
    # ------------------------------------------------------------------

    def _target_desc(self, kind: str) -> str:
        if kind == "tci":
            return f"{self.tci.host}:{self.tci.port} trx={self.tci.trx}"
        host = self._resolved_flex_host or "auto-discover"
        return f"{host}:{self.flex.port} slice={self.flex.slice_rx}"

    async def _build(self, kind: str) -> Optional[RadioConnection]:
        """Construct (but don't connect) the backend for ``kind``."""
        if kind == "flex":
            host = self._resolved_flex_host
            if not host:
                self._status(
                    "RADIO_CONNECTING",
                    "flex.host empty — discovering on UDP 4992 (up to 5s)…",
                )
                try:
                    disc = await flex_discover()
                except Exception as e:
                    logger.exception("Flex discovery raised")
                    self._status("RADIO_ERROR", f"discovery failed: {e}")
                    return None
                if disc and disc.get("ip"):
                    host = disc["ip"]
                    self._resolved_flex_host = host
                    logger.info("Flex: discovered radio at %s", host)
                else:
                    self._status("RADIO_ERROR",
                                 "no Flex answered discovery (powered on?)")
                    return None
            return FlexConnection(host, self.flex.port)

        if kind == "tci":
            if not self.tci.host:
                self._status("RADIO_ERROR", "tci.host is empty")
                return None
            return TciConnection(
                self.tci.host, self.tci.port,
                mode=self.tci.mode, tune_drive=self.tci.tune_drive,
            )

        self._status("RADIO_ERROR", f"unknown radio kind {kind!r}")
        return None
