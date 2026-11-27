"""Logout and named-session revocation (ARBORA-IDENTITY-R029.5).

Both doors end in one call: ``CALLER_REVOCATIONS.revoke_caller``, which closes every live
run lease of the principal at once. Logout reaches only the caller's own session. The served
operation ``identity.sessions.revoke`` names a session and needs ``identity:admin``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from arbora.security.run_authority import CALLER_REVOCATIONS, CallerRevocations

__all__ = [
    "SessionNotOwned",
    "revoke_own_session",
    "revoke_session",
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


async def revoke_session(
    context: Any,
    *,
    session_id: str,
    reason: str,
    admin: bool,
    registry: CallerRevocations | None = None,
) -> dict[str, Any]:
    """The served operation: revoke a named session of the tenant (admin only)."""
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.api.ops.engine_calls import decode, engine_send
    from arbora.api.ops.workstreams import _pack, _props
    from arbora.identity.client_session import session_node_id

    registry = registry or CALLER_REVOCATIONS
    if not admin:
        raise OperationRefused("SCOPE_MISSING")
    caller = context.caller
    tenant = str(caller.tenant)
    node = session_node_id(tenant, session_id)
    rows = decode(
        await engine_send(context, "GetNodePropertiesBatch", {"node_ids": [node]})
    )
    props = None
    for row in rows or ():
        if isinstance(row, (list, tuple)) and len(row) == 2 and row[0] == node:
            props = _props(row[1])
    if props is None or props.get("tenant") != tenant:
        raise OperationRefused(SessionNotOwned.code)
    principal = str(props.get("principal", ""))
    await engine_send(
        context,
        "CompareAndSetNodeFields",
        {
            "node_id": node,
            "conditions_msgpack": _pack({"principal": principal}),
            "updates_msgpack": _pack({"ended_at_ms": int(time.time() * 1000)}),
        },
    )
    stopped = registry.revoke_caller(tenant, principal, reason)
    services = context.operation_services
    if services is not None:
        from arbora.api.registry import AuditClass

        await services.audit_write(
            session_revocation_event(
                tenant=tenant,
                principal=principal,
                session_id=session_id,
                reason=reason,
                stopped=stopped,
                door="operation",
            ),
            AuditClass.EVENT,
            caller,
        )
    return {"value": {"revoked": True, "leases_stopped": stopped}}
