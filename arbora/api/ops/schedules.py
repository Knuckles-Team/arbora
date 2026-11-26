"""Schedule operations over the engine's durable job scheduler (ARBORA-OPS-R022.2.6 to R022.2.11).

A schedule is an engine ``AnalyticsJob`` schedule record. Arbora forwards the
caller's verified graph and claims, holds no schedule state, runs no scheduler
loop and does no group or spacing arithmetic: the engine decides ownership,
exclusion and skips, and its refusals keep their own codes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.invoke.bootstrap import caller_dispatch_graph
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops._engine import engine_send
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
    min_spacing_s: int = Field(default=0, ge=0, le=604_800)


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
