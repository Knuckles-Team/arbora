"""Catalog serving state machine (ARBORA-FLEET-R081).

The fleet catalog has one serving state: ``cold``, ``loading``, ``snapshot``,
``serving``, ``degraded`` or ``closing``; ``closing`` is the only terminal
state. A failed first read leaves ``cold``, which differs from an empty
catalog. ``degraded`` serves the stale snapshot. The readiness flags are pure
functions of the state; no site stores a readiness boolean.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from types import MappingProxyType

__all__ = [
    "CATALOG_EDGES",
    "CatalogEvent",
    "CatalogState",
    "IllegalCatalogTransition",
    "autoscaling_ready",
    "convergence_ready",
    "transition",
]


class CatalogState(enum.Enum):
    COLD = "cold"
    LOADING = "loading"
    SNAPSHOT = "snapshot"
    SERVING = "serving"
    DEGRADED = "degraded"
    CLOSING = "closing"


class CatalogEvent(enum.Enum):
    LOAD_START = "load_start"
    READ_OK = "read_ok"
    READ_ERR = "read_err"
    CHILDREN_UP = "children_up"
    EPOCH_BUMP = "epoch_bump"
    CLOSE = "close"


_S, _E = CatalogState, CatalogEvent
#: The 13 legal edges. ``read_err`` has two, split by the ``has_snapshot`` guard.
CATALOG_EDGES: Mapping[tuple[CatalogState, CatalogEvent, bool | None], CatalogState] = (
    MappingProxyType(
        {
            (_S.COLD, _E.LOAD_START, None): _S.LOADING,
            (_S.LOADING, _E.READ_OK, None): _S.SNAPSHOT,
            (_S.LOADING, _E.READ_ERR, False): _S.COLD,
            (_S.LOADING, _E.READ_ERR, True): _S.DEGRADED,
            (_S.SNAPSHOT, _E.CHILDREN_UP, None): _S.SERVING,
            (_S.SERVING, _E.EPOCH_BUMP, None): _S.LOADING,
            (_S.SNAPSHOT, _E.EPOCH_BUMP, None): _S.LOADING,
            (_S.DEGRADED, _E.LOAD_START, None): _S.LOADING,
            (_S.COLD, _E.CLOSE, None): _S.CLOSING,
            (_S.LOADING, _E.CLOSE, None): _S.CLOSING,
            (_S.SNAPSHOT, _E.CLOSE, None): _S.CLOSING,
            (_S.SERVING, _E.CLOSE, None): _S.CLOSING,
            (_S.DEGRADED, _E.CLOSE, None): _S.CLOSING,
        }
    )
)


class IllegalCatalogTransition(Exception):
    """A catalog event the machine refuses, or whose guard failed."""

    code = "CATALOG_TRANSITION_ILLEGAL"
    machine = "catalog_serving"

    def __init__(self, from_state: CatalogState, event: CatalogEvent) -> None:
        self.from_state = from_state
        self.event = event
        super().__init__(
            f"{self.code}: machine={self.machine} from_state={from_state.value} "
            f"event={event.value}"
        )


def transition(
    current: CatalogState, event: CatalogEvent, *, has_snapshot: bool = False
) -> CatalogState:
    """The one function that decides a catalog serving move."""
    target = CATALOG_EDGES.get((current, event, None))
    if target is None and event is _E.READ_ERR:
        target = CATALOG_EDGES.get((current, event, has_snapshot))
    if target is None:
        raise IllegalCatalogTransition(current, event)
    return target


def autoscaling_ready(state: CatalogState) -> bool:
    """Autoscaling may act only on a serving catalog."""
    return state is CatalogState.SERVING


def convergence_ready(state: CatalogState) -> bool:
    """Convergence may act only on a serving catalog."""
    return state is CatalogState.SERVING
