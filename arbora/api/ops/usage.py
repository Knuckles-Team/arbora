"""Usage read operation (ARBORA-OPS-R024.3).

``usage.read`` sources its numbers from the engine's own ``usage_reader``
port, not a counter Arbora keeps on the side. The OpSpec/refusal/params
plumbing lives in :mod:`arbora.api.ops._common`; this module owns only
the port protocol and the per-tenant totals shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from epistemic_graph import EngineResponseError
from pydantic import Field, ValidationError, model_validator

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.registry import OpSpec


class UsageReadParams(_common.Params):
    context_id: str | None = Field(default=None, min_length=1, max_length=512)


class UsageReadResult(_common.Params):
    value: dict[str, Any]


_Count = Annotated[int, Field(strict=True, ge=0, le=9_007_199_254_740_991)]
_RunCount = Annotated[int, Field(strict=True, ge=0, le=10_000)]
_CountName = Literal[
    "requests", "tool_calls", "input_tokens", "output_tokens", "total_tokens"
]


class SessionUsageSnapshot(_common.FrozenState):
    """The canonical EG result, validated without recomputing its totals."""

    context_id: str = Field(min_length=1, max_length=512)
    counts: dict[_CountName, _Count]
    expected_runs: _RunCount
    recorded_runs: _RunCount
    missing_runs: _RunCount
    count_coverage: dict[_CountName, _Count]
    inventory_complete: bool = Field(strict=True)

    @model_validator(mode="after")
    def _valid_coverage(self) -> SessionUsageSnapshot:
        if self.recorded_runs + self.missing_runs != self.expected_runs:
            raise ValueError("inconsistent session run coverage")
        if set(self.counts) != set(self.count_coverage):
            raise ValueError("session count coverage is missing")
        if any(
            not 1 <= value <= self.recorded_runs
            for value in self.count_coverage.values()
        ):
            raise ValueError("session count coverage exceeds recorded runs")
        if bool(self.counts) != bool(self.recorded_runs):
            raise ValueError("recorded runs require observed counters")
        if sum(self.count_coverage.values()) < self.recorded_runs:
            raise ValueError("session count coverage omits recorded runs")
        return self


class UsageSnapshot(_common.TenantState):
    """A tenant-scoped usage snapshot; the reader owns its exact shape."""

    totals: dict[str, float] = Field(default_factory=dict)


@runtime_checkable
class UsageReader(Protocol):
    """The one typed port Arbora calls the engine's usage method through."""

    async def read(self, *, tenant: str) -> UsageSnapshot: ...


async def handle_usage_read(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return this tenant's usage snapshot through the composed reader."""
    return await _read_snapshot(context, params.get("context_id"))


async def _read_snapshot(context: Any, context_id: str | None) -> dict[str, Any]:
    """Choose the requested canonical scope without a tenant fallback."""
    if context_id is not None:
        return await _read_session(context, context_id)
    return await _common.handle_tenant_read(
        context,
        service_name="usage_reader",
        unavailable_reason="usage reader is not composed",
    )


async def _read_session(context: Any, context_id: str) -> dict[str, Any]:
    from arbora.api.invoke.bootstrap import _caller_dispatch_graph
    from arbora.api.invoke.steps import claim_values
    from arbora.deployment.semantic_provisioning import CONTROL_GRAPH
    from arbora.security.anonymous import client_scope

    # Validate the current caller before selecting the host-owned ledger graph.
    # The process/service identity and caller-provided graph are never substitutes.
    caller_graph = await _caller_dispatch_graph(context)
    work_items = getattr(context.client, "work_items", None)
    reader = getattr(work_items, "session_usage", None)
    if not callable(reader):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "native session usage reader is unavailable"}
        )
    try:
        with client_scope(context.client, claim_values(context.caller.engine_claims)):
            raw = await reader(
                tenant=context.caller.tenant, context_id=context_id, graph=CONTROL_GRAPH
            )
        current_graph = await _caller_dispatch_graph(context)
        if current_graph != caller_graph:
            raise OperationRefused("UNAUTHENTICATED")
        snapshot = SessionUsageSnapshot.model_validate(raw)
        if snapshot.context_id != context_id:
            raise ValueError("session usage context mismatch")
    except EngineResponseError as exc:
        # Composite handlers do not pass through the direct-EG translator.
        raise EngineRefusal(exc.code) from exc
    except (ImportError, AttributeError) as exc:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "native session usage reader is unavailable"}
        ) from exc
    except (ValidationError, TypeError, ValueError) as exc:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "native session usage result is invalid"}
        ) from exc
    return {"value": {"scope": "session", **snapshot.model_dump(mode="json")}}


def operations() -> tuple[OpSpec, ...]:
    return (
        _common.build_tenant_read_op(
            op_id="usage.read",
            summary="Show this tenant's engine-sourced usage data",
            examples=("show usage for this tenant",),
            params=UsageReadParams,
            result=UsageReadResult,
            handler="arbora.api.ops.usage.handle_usage_read",
            scope="usage:read",
        ),
    )


specs = operations

__all__ = [
    "UsageReadParams",
    "UsageReadResult",
    "UsageReader",
    "UsageSnapshot",
    "handle_usage_read",
    "operations",
    "specs",
]
