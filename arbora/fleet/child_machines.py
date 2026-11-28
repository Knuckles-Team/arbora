"""The two explicit state machines of one multiplexer child (ARBORA-FLEET-R091).

``ChildState`` is the supervised connection lifecycle of a ``ChildRuntime``;
``BreakerState`` is the transport circuit breaker of the same child. Each has one
literal edge table, ``INITIAL``, ``TERMINALS`` and one transition function; an
event the state does not accept raises a typed refusal carrying ``machine``,
``from_state`` and ``event``. The stored and exported strings are the enum values
(the members are ``StrEnum``, so ``state == "up"`` and the JSON health snapshot
are unchanged). ``ChildRuntime._fire`` and ``_CircuitBreaker._fire`` in
``child_resilience.py`` are the only writers of the state fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

CHILD_TRANSITION_ILLEGAL = "CHILD_TRANSITION_ILLEGAL"
BREAKER_TRANSITION_ILLEGAL = "BREAKER_TRANSITION_ILLEGAL"


class ChildState(StrEnum):
    """Where a supervised child connection is in its life."""

    STARTING = "starting"
    UP = "up"
    RESTARTING = "restarting"
    FAILED = "failed"
    CLOSED = "closed"


class ChildEvent(StrEnum):
    """What happens to a child connection."""

    CONNECT = "connect"
    DISCONNECT = "disconnect"
    FAIL = "fail"
    CLOSE = "close"


_C, _CE = ChildState, ChildEvent

#: A new runtime starts here and waits for its first generation.
CHILD_INITIAL = _C.STARTING
#: Closed is the end of a runtime. Failed is parked (calls fail fast) but is closed by its owner.
CHILD_TERMINALS = (_C.CLOSED,)

#: Every legal (state, event) pair and the state it leads to. ``connect`` is a generation
#: that came up (or sessions adopted from an outside owner); ``disconnect`` is a lost or
#: recycled generation that will reconnect; ``fail`` is a boot failure or an exhausted
#: restart budget; ``close`` is the owner shutting the runtime down.
CHILD_EDGES: Mapping[tuple[ChildState, ChildEvent], ChildState] = MappingProxyType(
    {
        (_C.STARTING, _CE.CONNECT): _C.UP,
        (_C.UP, _CE.CONNECT): _C.UP,
        (_C.RESTARTING, _CE.CONNECT): _C.UP,
        (_C.UP, _CE.DISCONNECT): _C.RESTARTING,
        (_C.RESTARTING, _CE.DISCONNECT): _C.RESTARTING,
        (_C.STARTING, _CE.FAIL): _C.FAILED,
        (_C.UP, _CE.FAIL): _C.FAILED,
        (_C.RESTARTING, _CE.FAIL): _C.FAILED,
        (_C.STARTING, _CE.CLOSE): _C.CLOSED,
        (_C.UP, _CE.CLOSE): _C.CLOSED,
        (_C.RESTARTING, _CE.CLOSE): _C.CLOSED,
        (_C.FAILED, _CE.CLOSE): _C.CLOSED,
    }
)


class ChildTransitionRefused(RuntimeError):
    """An event the child state does not accept; the state does not change."""

    machine = "child_runtime"

    def __init__(self, state: ChildState, event: ChildEvent) -> None:
        self.code = CHILD_TRANSITION_ILLEGAL
        self.from_state = state
        self.event = event
        super().__init__(
            f"{CHILD_TRANSITION_ILLEGAL}: state {state.value} does not accept event {event.value}"
        )


def child_transition(state: ChildState, event: ChildEvent) -> ChildState:
    """The state ``event`` leads to from ``state``, or :class:`ChildTransitionRefused`."""
    try:
        return CHILD_EDGES[(state, event)]
    except KeyError:
        raise ChildTransitionRefused(state, event) from None


class BreakerState(StrEnum):
    """Where a child's transport circuit breaker is."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class BreakerEvent(StrEnum):
    """What happens to a circuit breaker."""

    TRIP = "trip"
    PROBE = "probe"
    RECOVER = "recover"


_B, _BE = BreakerState, BreakerEvent

#: A new breaker lets calls through.
BREAKER_INITIAL = _B.CLOSED
#: The breaker cycles for as long as the child runs; no state is final.
BREAKER_TERMINALS: tuple[BreakerState, ...] = ()

#: Every legal (state, event) pair. ``trip`` opens it (a failed probe, or the failure
#: threshold; a call that was already in flight may fail while open); ``probe`` is the
#: first call after the cooldown; ``recover`` is a success, which also closes a breaker
#: that was opened by calls still in flight.
BREAKER_EDGES: Mapping[tuple[BreakerState, BreakerEvent], BreakerState] = (
    MappingProxyType(
        {
            (_B.CLOSED, _BE.TRIP): _B.OPEN,
            (_B.OPEN, _BE.TRIP): _B.OPEN,
            (_B.HALF_OPEN, _BE.TRIP): _B.OPEN,
            (_B.OPEN, _BE.PROBE): _B.HALF_OPEN,
            (_B.CLOSED, _BE.RECOVER): _B.CLOSED,
            (_B.OPEN, _BE.RECOVER): _B.CLOSED,
            (_B.HALF_OPEN, _BE.RECOVER): _B.CLOSED,
        }
    )
)


class BreakerTransitionRefused(RuntimeError):
    """An event the breaker state does not accept; the state does not change."""

    machine = "child_breaker"

    def __init__(self, state: BreakerState, event: BreakerEvent) -> None:
        self.code = BREAKER_TRANSITION_ILLEGAL
        self.from_state = state
        self.event = event
        super().__init__(
            f"{BREAKER_TRANSITION_ILLEGAL}: state {state.value} does not accept event {event.value}"
        )


def breaker_transition(state: BreakerState, event: BreakerEvent) -> BreakerState:
    """The state ``event`` leads to from ``state``, or :class:`BreakerTransitionRefused`."""
    try:
        return BREAKER_EDGES[(state, event)]
    except KeyError:
        raise BreakerTransitionRefused(state, event) from None
