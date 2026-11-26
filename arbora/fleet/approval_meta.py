"""The approval reference a connector call carries (ARBORA-OPS-R009.5).

One helper at the one chokepoint: every child call path (the dedicated
service-credential session, the OAuth-gated session, and the fleet gateway's
delegated and service calls) builds its ``tools/call`` ``_meta`` here, so the
key and its shape exist once.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from arbora.access.approval_leases import ACTION_APPROVAL_KIND, APPROVAL_ID_PREFIX

__all__ = [
    "APPROVAL_META_KEY",
    "ApprovalRefused",
    "approval_call_kwargs",
    "approval_kwargs",
    "checked_approval_ref",
]

#: The ``_meta`` key a connector reads the approval reference from.
APPROVAL_META_KEY = "arbora/approval_ref"


def approval_call_kwargs(approval_ref: str) -> dict[str, dict[str, str]]:
    """The ``meta`` keyword of one connector ``tools/call``; empty without approval."""
    return {"meta": {APPROVAL_META_KEY: approval_ref}} if approval_ref else {}


def approval_kwargs(approval_ref: str) -> dict[str, str]:
    """Keyword arguments a transport receives; empty when no approval exists."""
    return {"approval_ref": approval_ref} if approval_ref else {}


class ApprovalRefused(Exception):
    """A denied, expired, pending, wrong-operation or absent approval.

    ``code`` is a member of the closed ``ArboraErrorCode`` enum; the caller
    converts it to the served refusal before any connector call is made.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _refusal(lease: Any, op_id: str, digest: str, now_ms: int) -> str | None:
    """The closed refusal code of one approval lease, or ``None`` when granted."""
    if not isinstance(lease, Mapping) or lease.get("kind") != ACTION_APPROVAL_KIND:
        return "CONFIRMATION_REQUIRED"
    status = lease.get("status")
    if status == "revoked":
        return "POLICY_DENIED"
    try:
        deadline = min(int(lease["expires_at_ms"]), int(lease["hard_expires_at_ms"]))
    except (KeyError, TypeError, ValueError):
        return "PLAN_EXPIRED"
    if status == "consumed":
        # A decided approval stays readable; only the span can expire it.
        pass
    elif status == "active":
        return "PLAN_EXPIRED" if now_ms >= deadline else "CONFIRMATION_REQUIRED"
    else:
        return "PLAN_EXPIRED"
    if now_ms >= deadline:
        return "PLAN_EXPIRED"
    grant = lease.get("grant")
    if not isinstance(grant, Mapping) or grant.get("op_id") != op_id:
        return "PLAN_MISMATCH"
    if digest and grant.get("request_digest") != digest:
        return "PLAN_MISMATCH"
    return None


async def _plan_needs_approval(leases: Any, tenant: str, plan_ref: str) -> bool:
    """Whether the plan was gated by the engine, so its approval must exist."""
    plan = await leases.get(tenant=tenant, lease_id=plan_ref)
    grant = plan.get("grant") if isinstance(plan, Mapping) else None
    return not isinstance(grant, Mapping) or grant.get("gate_source") != "default"


async def checked_approval_ref(
    leases: Any,
    *,
    tenant: str,
    idempotency_key: str | None,
    op_id: str,
    params_digest: str = "",
    now_ms: int | None = None,
) -> str:
    """Read the approval lease of a plan-confirmed call and return its reference.

    A call that carries no ``plan:<ref>`` key has no approval and returns
    ``""``. A plan the engine gated must hold a granted approval of this
    operation and digest that has not expired.

    Raises:
        ApprovalRefused: the approval is absent, still pending, denied,
            expired or bound to another operation.
    """
    key = idempotency_key or ""
    if not key.startswith("plan:"):
        return ""
    plan_ref = key[len("plan:") :]
    approval_id = APPROVAL_ID_PREFIX + plan_ref.removeprefix("arbora_plan:")
    lease = await leases.get(tenant=tenant, lease_id=approval_id)
    if lease is None and not await _plan_needs_approval(leases, tenant, plan_ref):
        return ""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    code = _refusal(lease, op_id, params_digest, now)
    if code is not None:
        raise ApprovalRefused(code)
    return approval_id
