"""Fail-closed errors for the Web UI control-plane boundary."""

from __future__ import annotations

from typing import ClassVar

__all__ = [
    "WebUiAuthorizationError",
    "WebUiCasConflictError",
    "WebUiDomainError",
    "WebUiEntityNotFoundError",
    "WebUiPaginationError",
    "WebUiPilotBoundaryError",
    "WebUiRetentionError",
    "WebUiTransitionRefused",
]


class WebUiDomainError(ValueError):
    """Base error whose stable code can be recorded without leaking data."""

    code: ClassVar[str] = "webui_domain_error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)


class WebUiAuthorizationError(WebUiDomainError):
    """The supplied context cannot access the requested tenant/workspace."""

    code = "webui_authorization_denied"


class WebUiCasConflictError(WebUiDomainError):
    """The caller supplied a stale or invalid monotonic version."""

    code = "webui_cas_conflict"


class WebUiEntityNotFoundError(WebUiDomainError):
    """A retention transition targeted no entity in the caller's scope."""

    code = "webui_entity_not_found"


class WebUiPaginationError(WebUiDomainError):
    """A cursor or page request was not bounded and canonical."""

    code = "webui_pagination_invalid"


class WebUiRetentionError(WebUiDomainError):
    """A lifecycle transition would bypass retention or legal-hold policy."""

    code = "webui_retention_invalid"


class WebUiPilotBoundaryError(WebUiDomainError):
    """An anonymous pilot operation crossed its private boundary."""

    code = "webui_anonymous_pilot_boundary"


class WebUiTransitionRefused(WebUiDomainError):
    """An identity's state machine lists no edge for this change; the entity is unchanged.

    ``machine`` names the identity type; ``from_state`` and ``event`` are the stored
    forms. A replacement whose state differs from the stored one by no listed edge
    carries ``event`` ``replace``.
    """

    code = "webui_transition_refused"

    def __init__(self, machine: str, from_state: str, event: str) -> None:
        super().__init__(f"{self.code}: {machine} {from_state} --{event}-->")
        self.machine = machine
        self.from_state = from_state
        self.event = event
