"""Radio backend abstraction for the SPE tune orchestrator.

spe-remote drives an external rig to key a clean carrier while the SPE's
ATU sweeps. Originally that rig was always a FlexRadio 6000 over the
SmartSDR TCP API; this module factors out the small set of primitives the
:class:`spe.tune_orchestrator.TuneOrchestrator` actually needs, so other
radio families can be plugged in — notably Expert Electronics SunSDR over
the TCI protocol (see :mod:`spe.tci`).

A backend only has to do five things during a tune cycle:

  * open / close its control connection,
  * set the operating frequency and mode on a channel,
  * (optionally) set the tune-carrier power,
  * key the tune carrier on / off,
  * and snapshot / restore the operator's VFO around the cycle.

``channel`` is the backend's notion of "which receiver/slice to drive":
the Flex *slice* index or the TCI *trx* index. The orchestrator passes the
value straight through from config, so each backend interprets it natively.

Frequencies cross this interface in **MHz** (the orchestrator and the SPE
band table work in MHz); a backend converts to its own wire unit (TCI uses
Hz, for instance). Modes cross as the generic string ``"CW"``; a backend
maps it to whatever its protocol wants (Flex wants ``CWU``).
"""

from __future__ import annotations

import abc
from typing import Optional


class RadioConnection(abc.ABC):
    """Minimal control surface the tune orchestrator needs from a rig."""

    #: Firmware / version string the radio reported on connect (best
    #: effort; empty until known). Surfaced in status messages.
    radio_version: str = ""

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool:
        """True while the control connection is open and usable."""

    @abc.abstractmethod
    async def connect(self) -> None:
        """Open the control connection. Raises on failure."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Close the control connection. Idempotent; must not raise."""

    @abc.abstractmethod
    async def set_frequency(self, channel: int, freq_mhz: float) -> None:
        """Tune ``channel`` to ``freq_mhz`` (MHz)."""

    @abc.abstractmethod
    async def set_mode(self, channel: int, mode: str) -> None:
        """Set ``channel`` to ``mode`` (generic, e.g. ``"CW"``)."""

    @abc.abstractmethod
    async def set_tune_power(self, watts: int) -> None:
        """Set the tune-carrier power in watts. A backend that has no
        wire control for this (power set in the radio's own UI) may treat
        it as a no-op."""

    @abc.abstractmethod
    async def tune_carrier(self, on: bool) -> None:
        """Key (on=True) or unkey (on=False) the built-in tune carrier."""

    @abc.abstractmethod
    def snapshot(self, channel: int) -> Optional[dict]:
        """Capture ``channel``'s current freq+mode so it can be restored
        after the cycle. Returns an opaque dict for :meth:`restore`, or
        None when the backend doesn't yet know the state (restore is then
        skipped). Synchronous: reads a cache populated by the backend's
        own event stream."""

    @abc.abstractmethod
    async def restore(self, snap: Optional[dict]) -> None:
        """Write a :meth:`snapshot` result back. No-op when ``snap`` is
        None. Best effort — should log rather than raise."""
