"""Logout and named-session revocation (ARBORA-IDENTITY-R029.5).

Both doors end in one call: ``CALLER_REVOCATIONS.revoke_caller``, which closes every live
run lease of the principal at once. Logout reaches only the caller's own session. The served
operation ``identity.sessions.revoke`` names a session and needs ``identity:admin``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from arbora.security.run_authority import CALLER_REVOCATIONS, CallerRevocations

__all__ = [
    "SessionNotOwned",
    "revoke_own_session",
    "session_revocation_event",
]


class SessionNotOwned(Exception):
    """The session is another principal's, or absent: one code for both."""

    code = "SESSION_NOT_OWNED"

    def __init__(self) -> None:
        super().__init__(self.code)


def session_revocation_event(
    *,
    tenant: str,
    principal: str,
    session_id: str,
    reason: str,
    stopped: int,
    door: str,
) -> dict[str, str]:
    """One audit event; it holds identifiers and a count, never a credential."""
    return {
        "op": "identity.sessions.revoke",
        "door": door,
        "tenant": tenant,
        "principal": principal,
        "session_id": session_id,
        "reason": reason,
        "leases_stopped": str(stopped),
    }


def revoke_own_session(
    *,
    tenant: str,
    caller_principal: str,
    session_principal: str,
    session_id: str,
    emit: Callable[[Mapping[str, str]], None],
    reason: str = "logout",
    registry: CallerRevocations = CALLER_REVOCATIONS,
) -> int:
    """Logout: stop the leases of the caller's own session only."""
    if session_principal != caller_principal:
        raise SessionNotOwned
    stopped = registry.revoke_caller(tenant, caller_principal, reason)
    emit(
        session_revocation_event(
            tenant=tenant,
            principal=caller_principal,
            session_id=session_id,
            reason=reason,
            stopped=stopped,
            door="logout",
        )
    )
    return stopped
