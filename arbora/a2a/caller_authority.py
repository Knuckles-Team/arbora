"""Original-caller authority for background tool tasks (ARBORA-A2A-R002.2.3.3).

At admission the WorkItem metadata stores ``authority_ref``, the id of an engine
delegation record (tenant, principal, granted tool, ``expires_at``). Arbora keeps
no credential. Before any effect, :func:`resolve_original_caller` reads that
record from the engine (``GetDelegationRecord``, MEDULLA-IDENTITY-R701) and
confirms that the principal is active, the record is unexpired and unrevoked,
and the principal still holds the tool scope. Anything else refuses with one of
two reasons and no connector call happens.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .tool_task import ToolTaskRefused, ToolTaskRequest

__all__ = [
    "CALLER_AUTHORITY_EXPIRED",
    "CALLER_AUTHORITY_REVOKED",
    "CallerAuthorityRefused",
    "OriginalCaller",
    "resolve_original_caller",
]

CALLER_AUTHORITY_REVOKED = "caller_authority_revoked"
CALLER_AUTHORITY_EXPIRED = "caller_authority_expired"

#: Reads one delegation record by its id. It returns the engine's
#: ``DelegationState`` mapping, or ``None`` when the engine answers
#: ``DELEGATION_UNKNOWN`` (a foreign-tenant record answers the same way).
DelegationReader = Callable[[str], Mapping[str, Any] | None]


class CallerAuthorityRefused(ToolTaskRefused):
    """The original caller no longer holds authority; no effect may run."""


@dataclass(frozen=True, slots=True)
class OriginalCaller:
    """The principal a tool task acts for, proven current by the engine."""

    tenant: str
    principal: str
    tool: str
    authority_ref: str


def resolve_original_caller(
    request: ToolTaskRequest, *, read_delegation: DelegationReader
) -> OriginalCaller:
    """Prove the request's original caller still holds authority, or refuse."""
    try:
        state = read_delegation(request.authority_ref)
    except Exception as exc:  # noqa: BLE001 - an unreadable record grants nothing
        raise CallerAuthorityRefused(CALLER_AUTHORITY_REVOKED) from exc
    if not isinstance(state, Mapping):
        raise CallerAuthorityRefused(CALLER_AUTHORITY_REVOKED)
    record = state.get("record")
    if not isinstance(record, Mapping):
        raise CallerAuthorityRefused(CALLER_AUTHORITY_REVOKED)
    if (
        record.get("tenant") != request.tenant
        or record.get("principal") != request.principal
        or record.get("tool") != request.operation_id
    ):
        raise CallerAuthorityRefused(CALLER_AUTHORITY_REVOKED)
    standing = state.get("standing")
    if standing == "expired":
        raise CallerAuthorityRefused(CALLER_AUTHORITY_EXPIRED)
    if (
        standing != "active"
        or record.get("revoked_at") is not None
        or state.get("principal_active") is not True
        or state.get("holds_tool_scope") is not True
    ):
        raise CallerAuthorityRefused(CALLER_AUTHORITY_REVOKED)
    return OriginalCaller(
        tenant=request.tenant,
        principal=request.principal,
        tool=request.operation_id,
        authority_ref=request.authority_ref,
    )
