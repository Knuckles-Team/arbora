"""Usage read operation (ARBORA-OPS-R024.3).

``usage.read`` sources its numbers from the engine's own ``usage_reader``
port, not a counter Arbora keeps on the side. The OpSpec/refusal/params
plumbing lives in :mod:`arbora.api.ops._common`; this module owns only
the port protocol and the per-tenant totals shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import Field, ValidationError, model_validator

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.registry import OpSpec


class UsageReadParams(_common.Params):
    context_id: str | None = Field(default=None, min_length=1, max_length=512)


class UsageReadResult(_common.Params):
    value: dict[str, Any]


_Count = Annotated[int, Field(strict=True, ge=0, le=9_007_199_254_740_991)]
_CountName = Literal[
    "requests", "tool_calls", "input_tokens", "output_tokens", "total_tokens"
]


class SessionUsageSnapshot(_common.FrozenState):
    """The canonical EG result, validated without recomputing its totals."""

    context_id: str = Field(min_length=1, max_length=512)
    counts: dict[_CountName, _Count]
    expected_runs: _Count
    recorded_runs: _Count
    missing_runs: _Count
    count_coverage: dict[_CountName, _Count]
    inventory_complete: bool = Field(strict=True)

    @model_validator(mode="after")
    def _valid_coverage(self) -> SessionUsageSnapshot:
        if self.recorded_runs + self.missing_runs != self.expected_runs:
            raise ValueError("inconsistent session run coverage")
        if set(self.counts) != set(self.count_coverage):
            raise ValueError("session count coverage is missing")
        if any(value > self.recorded_runs for value in self.count_coverage.values()):
            raise ValueError("session count coverage exceeds recorded runs")
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
    context_id = params.get("context_id")
    if context_id is not None:
        return await _read_session(context, str(context_id))
    return await _common.handle_tenant_read(
        context,
        service_name="usage_reader",
        unavailable_reason="usage reader is not composed",
    )


async def _read_session(context: Any, context_id: str) -> dict[str, Any]:
    # The shared invoke context is already bound to the verified caller. Do not
    # replace it with a process/service client or a reader-selected principal.
    work_items = getattr(context.client, "work_items", None)
    reader = getattr(work_items, "session_usage", None)
    if not callable(reader):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "native session usage reader is unavailable"}
        )
    try:
        raw = await reader(tenant=context.caller.tenant, context_id=context_id)
        snapshot = SessionUsageSnapshot.model_validate(raw)
        if snapshot.context_id != context_id:
            raise ValueError("session usage context mismatch")
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
