"""Run summary and current-plan read boundaries (ARBORA-OPS-R021.4.5).

``runs.current_plan`` serves the ``current_plan`` projection of one run. The
run harness owns the plan scaffold; Arbora owns admission and the served
read. Arbora stores no scaffold and never rebuilds one from stored messages.
An uncomposed reader, an unknown run and a run of another caller all return
the same ``unavailable`` result, which carries no items.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from agent_connector_sdk.current_plan import CurrentPlanRead, CurrentPlanRequest
from epistemic_graph.generated.models import HarnessConfigAnswer
from pydantic import Field, ValidationError

from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import FrozenState, Params, bound_service, build_read_op
from arbora.api.registry import OpSpec

UNAVAILABLE = CurrentPlanRead(availability="unavailable")


class RunSummary(FrozenState):
    """Recorded run fields; null means the producer supplied no measurement.

    Configuration is the recorded EG decision answer, including its abstentions.
    It is never resolved by asking for the caller's current configuration.
    """

    run_id: Annotated[str, Field(strict=True, min_length=1)]
    termination_reason: (
        Literal["final_answer", "context_overflow", "stuck", "step_budget", "error"]
        | None
    ) = None
    peak_context_tokens: Annotated[int, Field(strict=True, ge=0)] | None = None
    context_budget_tokens: Annotated[int, Field(strict=True, ge=0)] | None = None
    cost_usd: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)] | None = (
        None
    )
    harness_config_ref: Annotated[str, Field(strict=True, min_length=1)] | None = None
    harness_config: HarnessConfigAnswer | None = None
    current_plan: CurrentPlanRead = UNAVAILABLE


class RunsGetResult(Params):
    availability: Literal["available", "unavailable"]
    value: RunSummary | None


@runtime_checkable
class RunTraceReader(Protocol):
    """Per-run portion of the host's caller-bound durable trace reader."""

    async def get(
        self, *, run_id: str, context: ExecutionContext
    ) -> RunSummary | None: ...


async def handle_runs_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Read recorded values without reconstructing counters or decisions."""
    run_id = CurrentPlanRequest.model_validate(dict(params)).run_id
    reader = bound_service(context, "run_trace_reader", reason="run reader unavailable")
    raw = await reader.get(run_id=run_id, context=context)
    if raw is None:
        return RunsGetResult(availability="unavailable", value=None).model_dump(
            mode="json"
        )
    if not isinstance(raw, RunSummary):
        raise OperationRefused("UNAVAILABLE", {"reason": "invalid run projection"})
    try:
        summary = RunSummary.model_validate(raw.model_dump())
    except ValidationError as exc:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "invalid run projection"}
        ) from exc
    if summary.run_id != run_id or (
        summary.harness_config is not None and summary.harness_config_ref is None
    ):
        raise OperationRefused("UNAVAILABLE", {"reason": "invalid run projection"})
    return RunsGetResult(
        availability="available",
        value=summary.model_copy(
            update={"current_plan": await read_current_plan(context, run_id)}
        ),
    ).model_dump(mode="json")


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


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="runs.get",
            summary="Read recorded fields of one run",
            examples=("show this run's recorded context and cost",),
            params=CurrentPlanRequest,
            result=RunsGetResult,
            handler="arbora.api.ops.runs.handle_runs_get",
            scope="kg:read",
        ),
        build_read_op(
            op_id="runs.current_plan",
            summary="Read the current plan of one run",
            examples=("what is the current plan of this run",),
            params=CurrentPlanRequest,
            result=RunsCurrentPlanResult,
            handler="arbora.api.ops.runs.handle_runs_current_plan",
            scope="kg:read",
        ),
    )


specs = operations

__all__ = [
    "UNAVAILABLE",
    "CurrentPlanRead",
    "CurrentPlanReader",
    "CurrentPlanRequest",
    "RunSummary",
    "RunTraceReader",
    "RunsGetResult",
    "RunsCurrentPlanResult",
    "handle_runs_get",
    "handle_runs_current_plan",
    "operations",
    "project_current_plan",
    "read_current_plan",
    "specs",
]
