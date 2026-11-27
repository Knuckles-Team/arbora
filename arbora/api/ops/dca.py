"""Recurring DCA plans: ``dca.plans.set`` and ``dca.plans.pause`` (ARBORA-DATA-MARKET-R006).

A plan is one engine graph node ``dca-plan:<tenant>:<plan_id>`` that carries its
``revision``. Arbora holds no plan state: a write swaps the node only from the revision
the caller read, so two writers cannot both win (``CONFLICT``). The proposal key of a due
occurrence binds that revision (``arbora.api.domain.finance.dca.build_dca_proposal_key``), so a
revised plan never collides with its earlier proposals. The recurrence is the schedule
trigger grammar (``arbora.api.domain.finance.schedule.parse_trigger``), and the engine DCA evaluator
sizes each proposal (``arbora.api.domain.finance.dca.evaluate_dca``). The verified account binding
must match the plan's account.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import msgpack
from pydantic import Field, ValidationError

from arbora.api.domain.finance.dca import DCAEvaluationError, DCAPlan, minor_units
from arbora.api.domain.finance.schedule import ScheduleRefused, parse_trigger
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_write_op
from arbora.api.ops.engine_calls import decode, engine_send
from arbora.api.ops.finance import _authority
from arbora.api.registry import OpSpec

SCOPE = "finance:track"
#: The engine replays a plan write by the node swap it holds.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "dca.plans.set": "medulla:CompareAndSetNodeFields",
    "dca.plans.pause": "medulla:CompareAndSetNodeFields",
}
_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class DcaPlanSetParams(Params):
    plan_id: str = Field(pattern=_ID)
    symbol: str = Field(min_length=1, max_length=32)
    account: str = Field(min_length=1, max_length=256)
    amount: str = Field(pattern=r"^\d+(\.\d+)?$", max_length=64)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    timezone: str = Field(min_length=1, max_length=64)
    recurrence: str = Field(min_length=1, max_length=256)
    bounds: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["paper", "live"] = "paper"
    expected_revision: int = Field(default=0, ge=0)


class DcaPlanPauseParams(Params):
    plan_id: str = Field(pattern=_ID)
    paused: bool = True
    expected_revision: int = Field(ge=1)


class DcaPlanResult(Params):
    value: dict[str, Any]


def plan_node_id(tenant: str, plan_id: str) -> str:
    return f"dca-plan:{tenant}:{plan_id}"


def _pack(value: Mapping[str, Any]) -> bytes:
    return msgpack.packb(dict(value), use_bin_type=True)


def _props(blob: Any) -> dict[str, Any]:
    if isinstance(blob, (bytes, bytearray, memoryview)):
        blob = msgpack.unpackb(bytes(blob), raw=False)
    return dict(blob) if isinstance(blob, Mapping) else {}


async def _read(context: Any, node: str, tenant: str) -> dict[str, Any] | None:
    rows = decode(
        await engine_send(context, "GetNodePropertiesBatch", {"node_ids": [node]})
    )
    for row in rows or ():
        if (
            isinstance(row, (list, tuple))
            and len(row) == 2
            and row[0] == node
            and row[1]
        ):
            found = _props(row[1])
            # A foreign plan answers exactly like an absent one.
            return found if found.get("tenant_id") == tenant else None
    return None


def _took(answer: Any) -> bool:
    if isinstance(answer, Mapping):
        answer = answer.get("swapped", answer.get("updated", True))
    return answer is not False


def _plan(tenant: str, owner: str, params: Mapping[str, Any], revision: int) -> DCAPlan:
    try:
        plan = DCAPlan(
            plan_id=params["plan_id"],
            revision=revision,
            owner=owner,
            tenant_id=tenant,
            symbol=params["symbol"],
            account_id=params["account"],
            amount=Decimal(params["amount"]),
            currency=params["currency"],
            timezone=params["timezone"],
            recurrence_rule=params["recurrence"],
            mode=params["mode"],
        )
        parse_trigger(plan.recurrence_rule)
        from zoneinfo import ZoneInfo

        ZoneInfo(plan.timezone)
        minor_units(plan.amount, plan.currency)
    except (
        ValidationError,
        InvalidOperation,
        ScheduleRefused,
        DCAEvaluationError,
        ValueError,
        KeyError,
    ):
        raise OperationRefused("INVALID_ARGUMENT", {"field": "plan"}) from None
    return plan


async def handle_dca_plans_set(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    _port, tenant, account = await _authority(context)
    if params["account"] != account:
        raise OperationRefused("POLICY_DENIED", {"reason": "account binding mismatch"})
    node = plan_node_id(tenant, params["plan_id"])
    current = await _read(context, node, tenant)
    revision = int(current["revision"]) if current else 0
    if revision != params["expected_revision"]:
        raise OperationRefused("CONFLICT", {"revision": revision})
    plan = _plan(tenant, str(context.caller.principal), params, revision + 1)
    body = {
        **plan.model_dump(mode="json"),
        "type": "DcaPlan",
        "node_type": "DcaPlan",
        "bounds": dict(params["bounds"]),
    }
    if current is None:
        await engine_send(
            context,
            "CreateNodeIfAbsent",
            {"node_id": node, "properties_msgpack": _pack({**body, "revision": 0})},
        )
    won = await engine_send(
        context,
        "CompareAndSetNodeFields",
        {
            "node_id": node,
            "conditions_msgpack": _pack({"revision": revision}),
            "updates_msgpack": _pack(body),
        },
    )
    if not _took(won):
        raise OperationRefused("CONFLICT", {"revision": revision})
    return {
        "value": {"plan_id": plan.plan_id, "revision": plan.revision, "paused": False}
    }


async def handle_dca_plans_pause(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    tenant = str(context.caller.tenant)
    node = plan_node_id(tenant, params["plan_id"])
    current = await _read(context, node, tenant)
    if current is None:
        raise OperationRefused("NOT_FOUND")
    revision = int(current["revision"])
    if revision != params["expected_revision"]:
        raise OperationRefused("CONFLICT", {"revision": revision})
    won = await engine_send(
        context,
        "CompareAndSetNodeFields",
        {
            "node_id": node,
            "conditions_msgpack": _pack({"revision": revision}),
            "updates_msgpack": _pack(
                {"revision": revision + 1, "paused": params["paused"]}
            ),
        },
    )
    if not _took(won):
        raise OperationRefused("CONFLICT", {"revision": revision})
    return {
        "value": {
            "plan_id": params["plan_id"],
            "revision": revision + 1,
            "paused": params["paused"],
        }
    }


def operations() -> tuple[OpSpec, ...]:
    return (
        build_write_op(
            op_id="dca.plans.set",
            summary="Create or revise a recurring DCA plan",
            examples=("set up a monthly dollar-cost-averaging plan",),
            params=DcaPlanSetParams,
            result=DcaPlanResult,
            handler="arbora.api.ops.dca.handle_dca_plans_set",
            scope=SCOPE,
        ),
        build_write_op(
            op_id="dca.plans.pause",
            summary="Pause or resume a recurring DCA plan",
            examples=("pause my DCA plan",),
            params=DcaPlanPauseParams,
            result=DcaPlanResult,
            handler="arbora.api.ops.dca.handle_dca_plans_pause",
            scope=SCOPE,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "handle_dca_plans_pause",
    "handle_dca_plans_set",
    "operations",
    "plan_node_id",
    "specs",
]
