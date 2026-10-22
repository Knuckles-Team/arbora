"""Run read operations: the current-plan read boundary (ARBORA-OPS-R021.4.5.1).

``runs.current_plan`` serves the ``current_plan`` projection of one run. The
run harness owns the plan scaffold; Arbora owns admission and the served
read. Arbora stores no scaffold and never rebuilds one from stored messages.
An uncomposed reader, an unknown run and a run of another caller all return
the same ``unavailable`` result, which carries no items.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from agent_connector_sdk.current_plan import CurrentPlanRead, CurrentPlanRequest
from pydantic import ValidationError

from arbora.api.invoke.executor import ExecutionContext
from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops.run_replay import (
    SCHEMA,
    RunEventsParams,
    RunReadParams,
    unavailable,
)
from arbora.api.registry import OpSpec

UNAVAILABLE = CurrentPlanRead(availability="unavailable")


class RunsCurrentPlanResult(Params):
    value: dict[str, Any]


@runtime_checkable
class CurrentPlanReader(Protocol):
    """The host-composed read port for a run's live scaffold."""

    async def read(
        self, *, run_id: str, context: ExecutionContext
    ) -> CurrentPlanRead: ...


def project_current_plan(raw: object, *, run_id: str, now: datetime) -> CurrentPlanRead:
    """Validate a reader result; anything not current for ``run_id`` is unavailable."""
    if not isinstance(raw, CurrentPlanRead):
        return UNAVAILABLE
    try:
        read = CurrentPlanRead.model_validate(raw.model_dump())
    except ValidationError:
        return UNAVAILABLE
    snapshot = read.snapshot
    if snapshot is not None and (
        snapshot.run_id != run_id
        or snapshot.observed_at > now
        or snapshot.expires_at <= now
    ):
        return UNAVAILABLE
    return read


async def read_current_plan(
    context: Any,
    run_id: str,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CurrentPlanRead:
    """Read one run's plan for the verified caller of ``context``."""
    reader = context.services.get("current_plan_reader")
    if reader is None:
        return UNAVAILABLE
    raw = await reader.read(
        run_id=run_id,
        context=context,
    )
    return project_current_plan(raw, run_id=run_id, now=clock())


async def handle_runs_current_plan(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return the current plan of one run, or ``unavailable`` with no items."""
    run_id = CurrentPlanRequest.model_validate(dict(params)).run_id
    read = await read_current_plan(context, run_id)
    return {"value": read.model_dump(mode="json")}


async def handle_runs_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = RunReadParams.model_validate(dict(params))
    reader = context.services.get("run_trace_reader")
    value = (
        {
            "schema_version": SCHEMA,
            "availability": "unavailable",
            "run_id": request.run_id,
        }
        if reader is None
        else await reader.get(
            context=context, run_id=request.run_id, task_id=request.task_id
        )
    )
    value["current_plan"] = (
        await read_current_plan(context, request.run_id)
    ).model_dump(mode="json")
    return {"value": value}


async def handle_runs_events(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = RunEventsParams.model_validate(dict(params))
    reader = context.services.get("run_trace_reader")
    value = (
        unavailable(request.run_id)
        if reader is None
        else await reader.events(context=context, request=request)
    )
    return {"value": value}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="runs.current_plan",
            summary="Read the current plan of one run",
            examples=("what is the current plan of this run",),
            params=CurrentPlanRequest,
            result=RunsCurrentPlanResult,
            handler="arbora.api.ops.runs.handle_runs_current_plan",
            scope="kg:read",
        ),
        *(
            build_read_op(
                op_id=f"runs.{action}",
                summary=summary,
                examples=(summary,),
                params=params,
                result=RunsCurrentPlanResult,
                handler=f"arbora.api.ops.runs.handle_runs_{action}",
                scope="kg:read",
            )
            for action, summary, params in (
                ("get", "Read an owned durable run", RunReadParams),
                ("events", "Replay public events of an owned run", RunEventsParams),
            )
        ),
    )


specs = operations

__all__ = [
    "UNAVAILABLE",
    "CurrentPlanRead",
    "CurrentPlanReader",
    "CurrentPlanRequest",
    "RunsCurrentPlanResult",
    "handle_runs_current_plan",
    "operations",
    "project_current_plan",
    "read_current_plan",
    "specs",
]
