"""The fleet's owned run-time state (ARBORA-HOST-R039.2).

``FleetState`` is the ``fleet`` field of the host context. Each part owns its
lock and changes only through its own methods; the multiplexer takes the state
in its constructor and every other reader receives it by argument.
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass, field
from typing import Any

from arbora.fleet.declarations import Declarations
from arbora.fleet.gateway_ops import ConfirmingTools
from arbora.fleet.oauth_brokers import RemoteOAuthBrokers
from arbora.fleet.plan_confirmation import PlanConfirmations
from arbora.fleet.virtual_catalog import VirtualCatalog

__all__ = ["FleetState", "LiveMultiplexers"]


class LiveMultiplexers:
    """The multiplexers of one fleet whose child gauges a metrics scrape samples."""

    __slots__ = ("_live",)

    def __init__(self) -> None:
        self._live: weakref.WeakSet[Any] = weakref.WeakSet()

    def add(self, multiplexer: Any) -> None:
        self._live.add(multiplexer)

    def sample_child_health_gauges(self) -> None:
        """Refresh every live multiplexer's child gauges for a ``/metrics`` scrape.

        CONCEPT:MERISTEM-ECO.multiplexer.running-vs-dispatchable-metrics — child
        health is live-object state, not an event stream, so it must be sampled at
        scrape time or the gauges only ever move when somebody happens to call
        ``multiplexer_status``. ``status_snapshot()`` publishes the gauges itself,
        so calling it IS the sample.
        """
        for multiplexer in tuple(self._live):
            multiplexer.status_snapshot()


@dataclass(frozen=True, slots=True)
class FleetState:
    """Every owned run-time object of one fleet."""

    multiplexers: LiveMultiplexers = field(default_factory=LiveMultiplexers)
    oauth_brokers: RemoteOAuthBrokers = field(default_factory=RemoteOAuthBrokers)
    confirmations: PlanConfirmations = field(default_factory=PlanConfirmations)
    confirming_tools: ConfirmingTools = field(default_factory=ConfirmingTools)
    virtual_catalog: VirtualCatalog = field(default_factory=VirtualCatalog)
    declarations: Declarations = field(default_factory=Declarations)
