"""The per-tenant approval inbox (ARBORA-OPS-R918.1, R918.3).

Each gated plan is one engine ``action.approval`` control lease (filed when the
pipeline mints the ``plan_ref``, ARBORA-OPS-R918.2). The inbox lists and reads
those leases and ``approvals.decide`` moves one from ``active`` to ``consumed``
(approved) or ``revoked`` (denied) through the engine's lease transition.
Arbora keeps no second store. The decision is bound to the plan digest the
approver reviewed, the proposer can never approve its own item, and a repeated
decision returns the recorded outcome and changes nothing.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.access.approval_leases import (
    ACTION_APPROVAL_KIND,
    APPROVAL_ID_PREFIX,
    approval_lease_to_props,
)
from arbora.api.invoke.gate_class import GATE_CLASSES
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.registry import OpSpec

State = Literal["pending", "approved", "denied", "expired"]
_STATUS_OF: Mapping[str, str] = {
    "pending": "active",
    "expired": "active",
    "approved": "consumed",
    "denied": "revoked",
}
_DECISION_STATUS: Mapping[str, str] = {"approved": "consumed", "denied": "revoked"}
_STATE_OF_STATUS: Mapping[str, str] = {"consumed": "approved", "revoked": "denied"}
_MAX_PAGES = 16


class ApprovalsListParams(Params):
    gate_class: Literal[*GATE_CLASSES] | None = None  # type: ignore[valid-type]
    state: State | None = None
    approver_role: str | None = Field(default=None, min_length=1, max_length=128)
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class ApprovalsListResult(Params):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


class ApprovalsGetParams(Params):
    approval_id: str = Field(min_length=1, max_length=256)


class ApprovalsGetResult(Params):
    item: dict[str, Any]


class ApprovalsDigestParams(Params):
    since_ms: int = Field(default=0, ge=0)
    deliver: bool = False


class ApprovalsDigestResult(Params):
    by_class: list[dict[str, Any]]
    cursor_ms: int
    delivered: int = 0


class ApprovalsDecideParams(Params):
    approval_id: str = Field(min_length=1, max_length=256)
    decision: Literal["approved", "denied"]
    plan_digest: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)


class ApprovalsDecideResult(Params):
    item: dict[str, Any]
    changed: bool


def _now_ms(context: Any) -> int:
    clock = context.services.get("now_ms")
    return int(clock()) if callable(clock) else int(time.time() * 1000)


def _leases(context: Any) -> Any:
    return context.client.control_leases


def _caller_roles(caller: Any) -> frozenset[str]:
    roles = set(getattr(caller, "roles", None) or ())
    claims = getattr(caller, "engine_claims", None) or {}
    roles.update(claims.get("roles") or ())
    return frozenset(str(role) for role in roles)


def _state(lease: Mapping[str, Any], now_ms: int) -> str:
    status = str(lease.get("status", ""))
    if status in _STATE_OF_STATUS:
        return _STATE_OF_STATUS[status]
    deadline = min(
        int(lease.get("expires_at_ms", 0) or 0),
        int(lease.get("hard_expires_at_ms", 0) or 0),
    )
    return "expired" if status != "active" or now_ms >= deadline else "pending"


def _item(lease: Mapping[str, Any], now_ms: int) -> dict[str, Any]:
    props = approval_lease_to_props(lease)
    return {
        "approval_id": props.get("id"),
        "state": _state(lease, now_ms),
        "op": props.get("op_id"),
        "plan_digest": props.get("request_digest"),
        "proposer": props.get("proposer"),
        "gate_class": props.get("gate_class"),
        "approver_roles": list(props.get("approver_roles") or ()),
        "expires_at_ms": props.get("expires_at_ms"),
    }


def _visible(item: Mapping[str, Any], caller: Any, roles: frozenset[str]) -> bool:
    """An approver sees its items; a proposer sees its own proposals."""
    return item["proposer"] == caller.principal or bool(
        roles & set(item["approver_roles"])
    )


async def _read(context: Any, approval_id: str) -> Mapping[str, Any]:
    if not approval_id.startswith(APPROVAL_ID_PREFIX):
        raise OperationRefused("INVALID_ARGUMENT", {"field": "approval_id"})
    lease = await _leases(context).get(
        tenant=context.caller.tenant, lease_id=approval_id
    )
    if not isinstance(lease, Mapping) or lease.get("kind") != ACTION_APPROVAL_KIND:
        raise OperationRefused("NOT_FOUND", {"approval_id": approval_id})
    return lease


async def handle_approvals_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Pending, decided and expired items the caller may see, filtered."""
    now_ms = _now_ms(context)
    roles = _caller_roles(context.caller)
    wanted = params.get("state")
    statuses = [_STATUS_OF[wanted]] if wanted else ["active", "consumed", "revoked"]
    items: list[dict[str, Any]] = []
    cursor = params.get("cursor")
    for status in statuses:
        page = await _leases(context).list(
            tenant=context.caller.tenant,
            kind=ACTION_APPROVAL_KIND,
            status=status,
            cursor=cursor if len(statuses) == 1 else None,
            limit=params["limit"],
        )
        cursor = page.get("next_cursor") if len(statuses) == 1 else None
        for lease in page.get("leases") or ():
            item = _item(lease, now_ms)
            if wanted and item["state"] != wanted:
                continue
            if params.get("gate_class") and item["gate_class"] != params["gate_class"]:
                continue
            role = params.get("approver_role")
            if role and role not in item["approver_roles"]:
                continue
            if _visible(item, context.caller, roles):
                items.append(item)
    return {"items": items[: params["limit"]], "next_cursor": cursor}


async def handle_approvals_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One item; an item the caller may not see answers as an absent one."""
    lease = await _read(context, params["approval_id"])
    item = _item(lease, _now_ms(context))
    if not _visible(item, context.caller, _caller_roles(context.caller)):
        raise OperationRefused("NOT_FOUND", {"approval_id": params["approval_id"]})
    return {"item": item}


async def handle_approvals_digest(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Pending counts and the oldest age per gate class, delivered to owners on request."""
    now_ms = _now_ms(context)
    counts: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        page = await _leases(context).list(
            tenant=context.caller.tenant,
            kind=ACTION_APPROVAL_KIND,
            status="active",
            cursor=cursor,
            limit=100,
        )
        for lease in page.get("leases") or ():
            item = _item(lease, now_ms)
            issued = int(lease.get("issued_at_ms", 0) or 0)
            if item["state"] != "pending" or issued < params["since_ms"]:
                continue
            row = counts.setdefault(
                str(item["gate_class"]),
                {"gate_class": item["gate_class"], "pending": 0, "oldest_age_ms": 0},
            )
            row["pending"] += 1
            row["oldest_age_ms"] = max(row["oldest_age_ms"], now_ms - issued)
        cursor = page.get("next_cursor")
        if not cursor:
            break
    by_class = [counts[name] for name in GATE_CLASSES if name in counts]
    delivered = 0
    if params.get("deliver"):
        deliver = context.services.get("approval_digest_delivery")
        if deliver is None:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "no digest delivery channel is bound"}
            )
        delivered = await deliver(by_class)
    return {"by_class": by_class, "cursor_ms": now_ms, "delivered": delivered}


async def handle_approvals_decide(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Approve or deny one pending item, bound to the reviewed plan digest."""
    now_ms = _now_ms(context)
    lease = await _read(context, params["approval_id"])
    item = _item(lease, now_ms)
    caller = context.caller
    if not set(item["approver_roles"]) & _caller_roles(caller):
        # A foreign or unauthorised item answers exactly like an absent one.
        raise OperationRefused("NOT_FOUND", {"approval_id": params["approval_id"]})
    if item["proposer"] == caller.principal:
        raise OperationRefused("SELF_APPROVAL", {"approval_id": params["approval_id"]})
    if item["plan_digest"] != params["plan_digest"]:
        raise OperationRefused("PLAN_MISMATCH", {"approval_id": params["approval_id"]})
    target = _DECISION_STATUS[params["decision"]]
    if lease.get("status") == target:
        return {"item": item, "changed": False}
    if item["state"] == "expired":
        raise OperationRefused("PLAN_EXPIRED", {"approval_id": params["approval_id"]})
    if item["state"] != "pending":
        raise OperationRefused("ALREADY_DECIDED", {"state": item["state"]})
    answer = await _leases(context).transition(
        tenant=caller.tenant,
        lease_id=params["approval_id"],
        expected_revision=int(lease["revision"]),
        to=target,
        idempotency_key=f"decide:{params['approval_id']}:{params['decision']}",
    )
    if answer.get("outcome") != "applied":
        raise OperationRefused(
            "REVISION_CONFLICT", {"approval_id": params["approval_id"]}
        )
    return {"item": {**item, "state": _STATE_OF_STATUS[target]}, "changed": True}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="approvals.list",
            summary="List pending, decided and expired approval items",
            examples=("what is waiting for my approval",),
            params=ApprovalsListParams,
            result=ApprovalsListResult,
            handler="arbora.api.ops.approvals.handle_approvals_list",
            scope="approvals:read",
        ),
        build_read_op(
            op_id="approvals.get",
            summary="Show one approval item",
            examples=("show this approval item",),
            params=ApprovalsGetParams,
            result=ApprovalsGetResult,
            handler="arbora.api.ops.approvals.handle_approvals_get",
            scope="approvals:read",
        ),
        build_read_op(
            op_id="approvals.digest",
            summary="Count pending approval items by gate class",
            examples=("how many approvals are waiting",),
            params=ApprovalsDigestParams,
            result=ApprovalsDigestResult,
            handler="arbora.api.ops.approvals.handle_approvals_digest",
            scope="approvals:read",
        ),
        build_write_op(
            op_id="approvals.decide",
            summary="Approve or deny one pending approval item",
            examples=("approve this plan",),
            params=ApprovalsDecideParams,
            result=ApprovalsDecideResult,
            handler="arbora.api.ops.approvals.handle_approvals_decide",
            scope="approvals:decide",
        ),
    )


specs = operations

__all__ = [
    "handle_approvals_decide",
    "handle_approvals_digest",
    "handle_approvals_get",
    "handle_approvals_list",
    "operations",
    "specs",
]
