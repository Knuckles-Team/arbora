"""Connector onboarding handshake state machine (ARBORA-FLEET-R080).

Each connector has one onboarding state: ``idle``, ``debouncing``, ``running``,
``backoff``, ``awaiting_approval`` or ``failed``. The machine has no terminal
state: ``failed`` leaves on the slow-retry edge once the process credential
lease is live. A held mass withdrawal is the event ``held``, never a substring
of an error message. ``attempts`` and ``last_error`` are data, not state.
"""

from __future__ import annotations

import enum

__all__ = [
    "ONBOARDING_EDGES",
    "IllegalOnboardingTransition",
    "OnboardingEvent",
    "OnboardingState",
    "transition",
]


class OnboardingState(enum.Enum):
    IDLE = "idle"
    DEBOUNCING = "debouncing"
    RUNNING = "running"
    BACKOFF = "backoff"
    AWAITING_APPROVAL = "awaiting_approval"
    FAILED = "failed"


class OnboardingEvent(enum.Enum):
    REGISTERED = "registered"
    DEBOUNCE_ELAPSED = "debounce_elapsed"
    RETRY_ELAPSED = "retry_elapsed"
    PASS_OK = "pass_ok"  # nosec B105 - an event name, not a credential
    PASS_ERR = "pass_err"  # nosec B105 - an event name, not a credential
    HELD = "held"
    APPROVED = "approved"
    AUTHORITY_LIVE = "authority_live"


_S, _E = OnboardingState, OnboardingEvent
#: The 11 legal edges: (state, event, guard) -> state. The guard is
#: ``attempts_left`` for ``pass_err``, ``lease_live`` for ``authority_live``
#: and ``None`` elsewhere; the value of the guard is the key's third part.
ONBOARDING_EDGES: dict[
    tuple[OnboardingState, OnboardingEvent, str | None, bool], OnboardingState
] = {
    (_S.IDLE, _E.REGISTERED, None, True): _S.DEBOUNCING,
    (_S.DEBOUNCING, _E.REGISTERED, None, True): _S.DEBOUNCING,
    (_S.DEBOUNCING, _E.DEBOUNCE_ELAPSED, None, True): _S.RUNNING,
    (_S.RUNNING, _E.REGISTERED, None, True): _S.RUNNING,
    (_S.RUNNING, _E.PASS_OK, None, True): _S.IDLE,
    (_S.RUNNING, _E.PASS_ERR, "attempts_left", True): _S.BACKOFF,
    (_S.RUNNING, _E.PASS_ERR, "attempts_left", False): _S.FAILED,
    (_S.RUNNING, _E.HELD, None, True): _S.AWAITING_APPROVAL,
    (_S.BACKOFF, _E.RETRY_ELAPSED, None, True): _S.RUNNING,
    (_S.AWAITING_APPROVAL, _E.APPROVED, None, True): _S.RUNNING,
    (_S.FAILED, _E.AUTHORITY_LIVE, "lease_live", True): _S.BACKOFF,
}


class IllegalOnboardingTransition(Exception):
    """An onboarding event the machine refuses, or whose guard failed."""

    code = "ONBOARDING_TRANSITION_ILLEGAL"
    machine = "fleet_onboarding"

    def __init__(self, from_state: OnboardingState, event: OnboardingEvent) -> None:
        self.from_state = from_state
        self.event = event
        super().__init__(
            f"{self.code}: machine={self.machine} from_state={from_state.value} "
            f"event={event.value}"
        )


def transition(
    current: OnboardingState,
    event: OnboardingEvent,
    *,
    attempts_left: bool = True,
    lease_live: bool = True,
) -> OnboardingState:
    """The one function that decides an onboarding move."""
    for (state, fired, guard, value), target in ONBOARDING_EDGES.items():
        if state is not current or fired is not event:
            continue
        if guard is None:
            return target
        observed = attempts_left if guard == "attempts_left" else lease_live
        if observed is value:
            return target
    raise IllegalOnboardingTransition(current, event)
