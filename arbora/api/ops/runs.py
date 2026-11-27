"""Run read operations: the current-plan read boundary (ARBORA-OPS-R021.4.5.1).

``runs.current_plan`` serves the ``current_plan`` projection of one run. The
run harness owns the plan scaffold; Arbora owns admission and the served
read. Arbora stores no scaffold and never rebuilds one from stored messages.
An uncomposed reader, an unknown run and a run of another caller all return
the same ``unavailable`` result, which carries no items.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import Field, ValidationError, model_validator
from scion_sdk.current_plan import CurrentPlanRead, CurrentPlanRequest

from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops.run_graph import (
    ExecutionGraphParams,
    ExecutionGraphResult,
    unavailable_graph,
)
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
class RunTraceReader(Protocol):
    """The host-composed read port for owned run traces (ARBORA-OPS-R021.4.1).

    Each method takes the verified ``context`` and a typed request; the reader
    keeps the caller's claims and the engine's values unchanged.
    """

    async def get(
        self, *, context: ExecutionContext, run_id: str, task_id: str | None = None
    ) -> dict[str, Any]: ...

    async def events(
        self, *, context: ExecutionContext, request: RunEventsParams
    ) -> dict[str, Any]: ...

    async def execution_graph(
        self, *, context: ExecutionContext, request: ExecutionGraphParams
    ) -> dict[str, Any]: ...

    async def metrics(
        self, *, context: ExecutionContext, request: RunMetricsParams
    ) -> dict[str, Any]: ...

    async def node(
        self, *, context: ExecutionContext, request: RunNodeParams
    ) -> dict[str, Any]: ...


class MetricsWindow(Params):
    start_ms: int = Field(ge=0, le=2**63 - 1, strict=True)
    end_ms: int = Field(ge=0, le=2**63 - 1, strict=True)

    @model_validator(mode="after")
    def ordered(self) -> MetricsWindow:
        if self.start_ms > self.end_ms:
            raise ValueError("the window starts after it ends")
        return self


class RunMetricsParams(Params):
    """The ``RunMetrics`` request: what to measure, how to group it, over when."""

    query: str = Field(min_length=1, max_length=128, strict=True)
    group_by: str | None = Field(
        default=None, min_length=1, max_length=128, strict=True
    )
    window: MetricsWindow | None = None


class MetricGroup(Params):
    key: str = Field(max_length=256, strict=True)
    value: float = Field(ge=0)
    trace_count: int = Field(ge=0, le=2**63 - 1, strict=True)

    @model_validator(mode="after")
    def finite(self) -> MetricGroup:
        if not math.isfinite(self.value):
            raise ValueError("a metric value is not finite")
        return self


class RunMetricsOutcome(Params):
    """The engine's aggregate: groups, or an abstention code and no groups."""

    query: str = Field(min_length=1, max_length=128, strict=True)
    groups: list[MetricGroup] = Field(default_factory=list, max_length=1000)
    abstained: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def abstention_has_no_groups(self) -> RunMetricsOutcome:
        if self.abstained is not None and self.groups:
            raise ValueError("an abstained outcome carries no groups")
        return self


class RunNodeParams(RunReadParams):
    node_id: str = Field(min_length=1, max_length=512, strict=True)


#: Capabilities a node read reports; each defaults to unavailable until the
#: producer issues an owner-bound target (ARBORA-OPS-R021.4.9).
NODE_CAPABILITIES = (
    "can_read_events",
    "can_read_transcript",
    "can_message_active",
    "can_continue_completed",
)


def unavailable_capabilities(
    reason: str = "no_owner_binding",
) -> dict[str, dict[str, Any]]:
    return {name: {"allowed": False, "reason": reason} for name in NODE_CAPABILITIES}


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


async def handle_runs_execution_graph(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = ExecutionGraphParams.model_validate(dict(params))
    reader = context.services.get("run_trace_reader")
    value = (
        unavailable_graph(request)
        if reader is None
        else await reader.execution_graph(context=context, request=request)
    )
    return {"value": value}


async def handle_runs_metrics(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return the engine's run aggregates unchanged, validated, with no arithmetic."""
    request = RunMetricsParams.model_validate(dict(params))
    reader = context.services.get("run_trace_reader")
    if reader is None:
        return {
            "value": {
                "schema_version": SCHEMA,
                "availability": "unavailable",
                "query": request.query,
            }
        }
    raw = await reader.metrics(context=context, request=request)
    try:
        outcome = RunMetricsOutcome.model_validate(raw)
    except ValidationError:
        raise OperationRefused(
            "INVALID_ARGUMENT", {"reason": "the engine outcome is not a valid metric"}
        ) from None
    return {
        "value": {
            "schema_version": SCHEMA,
            "availability": "available",
            **outcome.model_dump(mode="json"),
        }
    }


async def handle_runs_node(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = RunNodeParams.model_validate(dict(params))
    reader = context.services.get("run_trace_reader")
    value = (
        {
            "schema_version": SCHEMA,
            "availability": "unavailable",
            "run_id": request.run_id,
            "node_id": request.node_id,
            "capabilities": unavailable_capabilities(),
        }
        if reader is None
        else await reader.node(context=context, request=request)
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
        build_read_op(
            op_id="runs.execution_graph",
            summary="Read a bounded execution graph of an owned run, with declared nested runs",
            examples=("show the declared steps and evidenced lifecycle of this run",),
            params=ExecutionGraphParams,
            result=ExecutionGraphResult,
            handler="arbora.api.ops.runs.handle_runs_execution_graph",
            scope="kg:read",
        ),
        build_read_op(
            op_id="runs.metrics",
            summary="Read engine aggregates over run traces",
            examples=("what did runs cost last week, grouped by agent",),
            params=RunMetricsParams,
            result=RunsCurrentPlanResult,
            handler="arbora.api.ops.runs.handle_runs_metrics",
            scope="telemetry:read",
        ),
        build_read_op(
            op_id="runs.node",
            summary="Read one permitted node of an owned run with its capabilities",
            examples=("show this node of the run and what I may do with it",),
            params=RunNodeParams,
            result=RunsCurrentPlanResult,
            handler="arbora.api.ops.runs.handle_runs_node",
            scope="kg:read",
        ),
    )


specs = operations

__all__ = [
    "UNAVAILABLE",
    "CurrentPlanRead",
    "CurrentPlanReader",
    "CurrentPlanRequest",
    "RunMetricsOutcome",
    "RunMetricsParams",
    "RunNodeParams",
    "RunTraceReader",
    "RunsCurrentPlanResult",
    "handle_runs_current_plan",
    "handle_runs_metrics",
    "handle_runs_node",
    "operations",
    "project_current_plan",
    "read_current_plan",
    "specs",
]
