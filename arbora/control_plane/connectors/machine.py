"""Connector desired-status machine (ARBORA-FLEET-R082).

``DesiredStatus`` (``disabled``, ``enabled``, ``quarantined``) has one
transition function and no terminal state. ``quarantined`` leaves only through
``release``, which lands on ``disabled``. ``AuthorizationOutcome`` is a read-only
projection of the medulla decision record: arbora holds no transition table
for it and refuses a write that no decision supports.
"""

from __future__ import annotations

import enum
from typing import get_args

from arbora.control_plane.connectors.models import (
    AuthorizationDecision,
    AuthorizationOutcome,
    DesiredStatus,
)

__all__ = [
    "DesiredEvent",
    "IllegalDesiredTransition",
    "event_between",
    "project_outcome",
    "transition",
]


class DesiredEvent(enum.Enum):
    """The closed events of the desired-status machine."""

    ENABLE = "enable"
    DISABLE = "disable"
    QUARANTINE = "quarantine"
    RELEASE = "release"


_E = DesiredEvent
#: The 5 legal edges. Every other pair is refused.
DESIRED_EDGES: dict[tuple[DesiredStatus, DesiredEvent], DesiredStatus] = {
    ("disabled", _E.ENABLE): "enabled",
    ("enabled", _E.DISABLE): "disabled",
    ("disabled", _E.QUARANTINE): "quarantined",
    ("enabled", _E.QUARANTINE): "quarantined",
    ("quarantined", _E.RELEASE): "disabled",
}
DESIRED_STATES: tuple[str, ...] = get_args(DesiredStatus)


class IllegalDesiredTransition(Exception):
    """A desired-status event the machine refuses."""

    code = "CONNECTOR_TRANSITION_ILLEGAL"
    machine = "connector_desired_status"

    def __init__(self, from_state: str, event: DesiredEvent) -> None:
        self.from_state = from_state
        self.event = event
        super().__init__(
            f"{self.code}: machine={self.machine} from_state={from_state} "
            f"event={event.value}"
        )


def transition(current: DesiredStatus, event: DesiredEvent) -> DesiredStatus:
    """The one function that decides a desired-status move."""
    target = DESIRED_EDGES.get((current, event))
    if target is None:
        raise IllegalDesiredTransition(current, event)
    return target


def event_between(current: DesiredStatus, target: DesiredStatus) -> DesiredEvent | None:
    """The event that moves ``current`` to ``target``, or ``None`` for no move."""
    if current == target:
        return None
    for (source, event), result in DESIRED_EDGES.items():
        if source == current and result == target:
            return event
    # No event reaches it: report the closest refusal on the requested target.
    return {"enabled": _E.ENABLE, "disabled": _E.DISABLE, "quarantined": _E.QUARANTINE}[
        target
    ]


#: One decision state maps to one outcome. The states are the medulla
#: elevation statuses (MEDULLA-IDENTITY-R720); arbora keeps no table of moves.
_OUTCOME_BY_DECISION_STATE: dict[str, AuthorizationOutcome] = {
    "pending": "pending",
    "requested": "pending",
    "approved": "approved",
    "denied": "denied",
    "revoked": "revoked",
    "expired": "revoked",
}


def project_outcome(decision: AuthorizationDecision | str) -> AuthorizationOutcome:
    """Project a decision (or its engine state name) onto an outcome."""
    state = (
        decision.outcome if isinstance(decision, AuthorizationDecision) else decision
    )
    try:
        return _OUTCOME_BY_DECISION_STATE[str(state)]
    except KeyError:
        raise ValueError(f"no outcome projects from decision state {state!r}") from None
