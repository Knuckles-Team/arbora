"""Schedule operations over the engine's durable job scheduler (ARBORA-OPS-R022.2.6 to R022.2.12).

A schedule is an engine ``AnalyticsJob`` schedule record. Arbora forwards the
caller's verified graph and claims, holds no schedule state, runs no scheduler
loop and does no group or spacing arithmetic: the engine decides ownership,
exclusion and skips, and its refusals keep their own codes.

A schedule whose ``task_kind`` is ``roles.run`` names a ``role_instance_id`` in
its ``spec``. Arbora reads that instance from the engine agent library as the
caller and fills the token budget (from the instance usage limits) and the
exclusion group (the instance id: one active run per role instance) when the
caller gave none; the caller's own values win.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.invoke.bootstrap import caller_dispatch_graph
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.engine_calls import engine_send
from arbora.api.registry import Effect, OpSpec
from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_models

READ_SCOPE = "loops:read"
CONTROL_SCOPE = "loops:control"
#: The engine job op each operation sends, in the ``AnalyticsJob`` op envelope.
_ENGINE_OPS = {
    "schedules.create": "ScheduleCreate",
    "schedules.update": "ScheduleUpdate",
    "schedules.delete": "ScheduleDelete",
    "schedules.pause": "SchedulePause",
    "schedules.resume": "ScheduleResume",
    "schedules.runs": "ScheduleTicks",
}
_NAME = Field(min_length=1, max_length=128)


class ScheduleRefParams(Params):
    name: str = _NAME


class ScheduleWriteParams(Params):
    name: str = _NAME
    task_kind: str = Field(min_length=1, max_length=128)
    spec: dict[str, Any] | str
    period_s: int | None = Field(default=None, ge=0)
    max_run_s: int | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=0)
    exclusion_group: str | None = Field(default=None, min_length=1, max_length=64)
    min_spacing_s: int | None = Field(default=None, ge=0, le=604_800)


class ScheduleRunsParams(ScheduleRefParams):
    limit: int = Field(default=50, ge=1, le=100)


class ScheduleResult(Params):
    value: dict[str, Any]


def _body(op_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
    if op_id in ("schedules.create", "schedules.update"):
        return {key: value for key, value in params.items() if value is not None}
    body: dict[str, Any] = {"name": params["name"]}
    if op_id == "schedules.runs":
        body["limit"] = params["limit"]
    return body


ROLE_TARGET = "roles.run"


def _instance_budget(entry: Mapping[str, Any]) -> int | None:
    """The token budget one run of the instance may use, from its usage limits."""
    runtime = entry.get("runtime")
    limits = runtime.get("usage_limits") if isinstance(runtime, Mapping) else None
    if not isinstance(limits, Mapping):
        return None
    parts = [limits.get("input_tokens_limit"), limits.get("output_tokens_limit")]
    if all(isinstance(part, int) for part in parts):
        return sum(parts)
    return next((part for part in parts if isinstance(part, int)), None)


async def _role_defaults(context: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Fill the budget and exclusion group from the named role instance binding."""
    spec = params.get("spec")
    instance = spec.get("role_instance_id") if isinstance(spec, Mapping) else None
    if not isinstance(instance, str) or not instance:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "spec.role_instance_id"})
    answer = await engine_send(
        context,
        "AgentLibrary",
        {
            "op": {
                "operation": "current",
                "agent_id": instance,
                "tenant_id": str(context.caller.tenant),
            }
        },
    )
    entry = answer.get("entry", answer) if isinstance(answer, Mapping) else None
    if not isinstance(entry, Mapping) or entry.get("agent_id") != instance:
        # An absent instance and a foreign one answer the same way.
        raise OperationRefused("NOT_FOUND")
    filled = dict(params)
    if filled.get("max_tokens") is None:
        filled["max_tokens"] = _instance_budget(entry)
    if filled.get("exclusion_group") is None:
        filled["exclusion_group"] = instance[:64]
    return filled


async def handle_schedule(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Send one schedule job op as the verified caller; the engine owns the answer."""
    engine_op = _ENGINE_OPS[op.id]
    if op.id == "schedules.runs":
        try:
            require_models(["JobOpScheduleTicks"])
        except PinLagUnavailable as error:
            raise refusal(error) from error
    if op.id in ("schedules.create", "schedules.update") and (
        params.get("task_kind") == ROLE_TARGET
    ):
        params = await _role_defaults(context, params)
    graph = await caller_dispatch_graph(context)
    body = {**_body(op.id, params), "graph": graph}
    answer = await engine_send(
        context,
        "AnalyticsJob",
        {"op": {engine_op: body}},
        idempotent=op.effect is not Effect.READ,
    )
    return {
        "value": dict(answer) if isinstance(answer, Mapping) else {"result": answer}
    }


def operations() -> tuple[OpSpec, ...]:
    handler = "arbora.api.ops.schedules.handle_schedule"
    common: dict[str, Any] = {
        "result": ScheduleResult,
        "handler": handler,
        "scope": CONTROL_SCOPE,
    }
    return (
        build_write_op(
            op_id="schedules.create",
            summary="Create one owned schedule",
            examples=("schedule this task",),
            params=ScheduleWriteParams,
            **common,
        ),
        build_write_op(
            op_id="schedules.update",
            summary="Change one owned schedule",
            examples=("change this schedule",),
            params=ScheduleWriteParams,
            **common,
        ),
        build_write_op(
            op_id="schedules.delete",
            summary="Delete one owned schedule",
            examples=("delete this schedule",),
            params=ScheduleRefParams,
            effect=Effect.ADMIN,
            **common,
        ),
        build_write_op(
            op_id="schedules.pause",
            summary="Pause one owned schedule",
            examples=("pause this schedule",),
            params=ScheduleRefParams,
            **common,
        ),
        build_write_op(
            op_id="schedules.resume",
            summary="Resume one owned schedule",
            examples=("resume this schedule",),
            params=ScheduleRefParams,
            **common,
        ),
        build_read_op(
            op_id="schedules.runs",
            summary="Show the tick history of one schedule",
            examples=("what did this schedule do",),
            params=ScheduleRunsParams,
            result=ScheduleResult,
            handler=handler,
            scope=READ_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "ScheduleRefParams",
    "ScheduleResult",
    "ScheduleRunsParams",
    "ScheduleWriteParams",
    "handle_schedule",
    "operations",
    "specs",
]
