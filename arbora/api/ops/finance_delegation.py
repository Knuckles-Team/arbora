"""The owner's delegation to the finance scheduler (ARBORA-DATA-MARKET-R002).

One mechanism: a delegation is an engine resource grant, the same ledger the meeting and
credential checks use. ``finance.delegation.grant`` writes it with ``RbacAdmin
GrantResource``; the scheduler asks ``CheckResourceAccess`` on the resource
``finance-delegation:<owner>`` (``arbora.finance.schedule_host``). Arbora keeps no
delegation node. The grantee is the host's scheduler principal; the operation is the
finance grant the entry runs under. The grant id is deterministic, so a replay writes the
same grant. Only the owner delegates their own work.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, bound_service, build_write_op
from arbora.api.registry import Confirm, OpSpec
from arbora.finance.delegation import (
    DELEGATION_PROTOCOL,
    delegation_grant_id,
    delegation_resource,
)

SCOPE = "finance:track"
SCHEDULER_BINDING = "finance_scheduler_principal"
#: The grant and its revocation are engine ``RbacAdmin`` writes the engine replays itself.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "finance.delegation.grant": "medulla:GrantResource",
    "finance.delegation.revoke": "medulla:RevokeResource",
}
_DAY_S = 86_400


class DelegationGrantParams(Params):
    grant: Literal["finance:backfill", "finance:track", "finance:read"]
    expires_in_s: int = Field(default=90 * _DAY_S, ge=60, le=365 * _DAY_S)


class DelegationRevokeParams(Params):
    grant: Literal["finance:backfill", "finance:track", "finance:read"]


class DelegationResult(Params):
    value: dict[str, Any]


def _now_ms(context: Any) -> int:
    clock = context.services.get("now_ms")
    return int(clock()) if callable(clock) else int(time.time() * 1000)


def _owner(context: Any) -> str:
    owner = getattr(context.caller, "principal", None)
    if not isinstance(owner, str) or not owner:
        raise OperationRefused("POLICY_DENIED", {"reason": "no verified owner"})
    return owner


async def _rbac_admin(context: Any, op: Mapping[str, Any], model: str) -> None:
    from arbora.pin_lag.medulla import send_main

    await send_main(
        context, "RbacAdmin", {"op": dict(op)}, idempotent=True, models=(model,)
    )


async def handle_delegation_grant(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    owner = _owner(context)
    scheduler = bound_service(
        context, SCHEDULER_BINDING, reason="the finance scheduler is not composed"
    )
    now = _now_ms(context)
    grant_id = delegation_grant_id(owner, params["grant"])
    await _rbac_admin(
        context,
        {
            "GrantResource": {
                "grant_id": grant_id,
                "subject": {"Principal": str(scheduler)},
                "resource_ref": delegation_resource(owner),
                "protocol": DELEGATION_PROTOCOL,
                "operation": params["grant"],
                "scope_ceiling": [params["grant"]],
                "granted_at_ms": now,
                "expires_at_ms": now + int(params["expires_in_s"]) * 1000,
            }
        },
        "ResourceGrant",
    )
    return {"value": {"grant_id": grant_id, "grant": params["grant"], "held": True}}


async def handle_delegation_revoke(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    grant_id = delegation_grant_id(_owner(context), params["grant"])
    await _rbac_admin(
        context,
        {"RevokeResource": {"grant_id": grant_id}},
        "RbacAdminOpRevokeResource",
    )
    return {"value": {"grant_id": grant_id, "grant": params["grant"], "held": False}}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_write_op(
            op_id="finance.delegation.grant",
            summary="Delegate a finance grant to the scheduler for the owner's schedules",
            examples=("let the scheduler run my finance jobs",),
            params=DelegationGrantParams,
            result=DelegationResult,
            handler="arbora.api.ops.finance_delegation.handle_delegation_grant",
            scope=SCOPE,
            confirm=Confirm.PLAN,
        ),
        build_write_op(
            op_id="finance.delegation.revoke",
            summary="Withdraw a finance grant from the scheduler",
            examples=("stop the scheduler running my finance jobs",),
            params=DelegationRevokeParams,
            result=DelegationResult,
            handler="arbora.api.ops.finance_delegation.handle_delegation_revoke",
            scope=SCOPE,
            confirm=Confirm.PLAN,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "handle_delegation_grant",
    "handle_delegation_revoke",
    "operations",
    "specs",
]
