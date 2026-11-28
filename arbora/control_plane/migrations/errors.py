"""Fail-closed errors for migration authority transitions."""

from __future__ import annotations

from typing import ClassVar

__all__ = [
    "MigrationAuthorizationError",
    "MigrationCasConflictError",
    "MigrationConflictError",
    "MigrationDomainError",
    "MigrationGateError",
    "MigrationNotFoundError",
    "MigrationPrerequisiteError",
    "MigrationReplayError",
    "MigrationTransitionRefused",
]


class MigrationDomainError(ValueError):
    """Stable, privacy-safe domain error with no source payload."""

    code: ClassVar[str] = "migration_domain_error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)


class MigrationAuthorizationError(MigrationDomainError):
    code = "migration_authorization_denied"


class MigrationCasConflictError(MigrationDomainError):
    code = "migration_cas_conflict"


class MigrationConflictError(MigrationDomainError):
    code = "migration_conflict"


class MigrationGateError(MigrationDomainError):
    code = "migration_gate_blocked"


class MigrationNotFoundError(MigrationDomainError):
    code = "migration_not_found"


class MigrationPrerequisiteError(MigrationDomainError):
    code = "migration_prerequisite_missing"


class MigrationReplayError(MigrationDomainError):
    code = "migration_replay_drift"


class MigrationTransitionRefused(MigrationPrerequisiteError):
    """A migration record machine refused an event that its table does not list.

    The record does not change.  ``machine`` names the record type, ``from_state``
    and ``event`` are the stored forms of the refused pair.
    """

    code = "migration_transition_refused"

    def __init__(
        self,
        machine: str,
        from_state: str,
        event: str,
        message: str | None = None,
    ) -> None:
        super().__init__(message or f"{self.code}: {machine} {from_state} --{event}-->")
        self.machine = machine
        self.from_state = from_state
        self.event = event
