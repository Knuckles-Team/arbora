"""Usage read operation (ARBORA-OPS-R024.3).

``usage.read`` sources its numbers from the engine, not a counter Arbora keeps
on the side. With a ``context_id`` it forwards the engine ``GetSessionUsage``
read for the caller's own tenant (ARBORA-OPS-R024.3.1) and returns the engine's
counts, coverage and per-turn timings unchanged (ARBORA-OPS-R024.3.2). Without
one it reads the tenant totals through the composed ``usage_reader`` port.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field, ValidationError

from arbora.api.ops import _common
from arbora.api.ops._engine import engine_send, unavailable
from arbora.api.registry import OpSpec

#: The longest elapsed time one turn timing may report, in milliseconds.
MAX_ELAPSED_MS = 86_400_000


class UsageReadParams(_common.Params):
    context_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$", max_length=128
    )


class TurnTiming(_common.Params):
    """One measured turn phase; client elapsed time, never a provider rate."""

    run_id: str
    turn_index: int = Field(ge=0)
    phase: Literal["request", "run", "queue", "tool"]
    duration_ms: int = Field(ge=0, le=MAX_ELAPSED_MS)


class SessionUsageView(_common.Params):
    context_id: str
    counts: dict[str, int]
    count_coverage: dict[str, int]
    expected_runs: int = Field(ge=0)
    recorded_runs: int = Field(ge=0)
    missing_runs: int = Field(ge=0)
    inventory_complete: bool
    turn_timings: list[TurnTiming] | None = None
    timing_refusal: Literal["TIMING_INVALID"] | None = None


class UsageReadResult(_common.Params):
    value: dict[str, Any] | None = None
    session: SessionUsageView | None = None


class UsageSnapshot(_common.TenantState):
    """A tenant-scoped usage snapshot; the reader owns its exact shape."""

    totals: dict[str, float] = Field(default_factory=dict)


@runtime_checkable
class UsageReader(Protocol):
    """The one typed port Arbora calls the engine's usage method through."""

    async def read(self, *, tenant: str) -> UsageSnapshot: ...


def _session_view(answer: Any) -> dict[str, Any]:
    """The engine answer as the typed view; a bad timing keeps the totals."""
    if not isinstance(answer, Mapping):
        raise unavailable("engine result malformed")
    fields = dict(answer)
    timings = fields.pop("turn_timings", None)
    try:
        view = SessionUsageView.model_validate(fields)
    except ValidationError:
        raise unavailable("engine result malformed") from None
    if timings is not None:
        try:
            known = [TurnTiming.model_validate(row) for row in timings]
        except (ValidationError, TypeError):
            return view.model_copy(
                update={"timing_refusal": "TIMING_INVALID"}
            ).model_dump(mode="json")
        view = view.model_copy(update={"turn_timings": known})
    return view.model_dump(mode="json")


async def handle_usage_read(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return one session's usage, or this tenant's snapshot through the reader."""
    if params.get("context_id") is not None:
        answer = await engine_send(
            context,
            "GetSessionUsage",
            {"context_id": params["context_id"], "tenant": str(context.caller.tenant)},
        )
        return {"session": _session_view(answer)}
    return await _common.handle_tenant_read(
        context,
        service_name="usage_reader",
        unavailable_reason="usage reader is not composed",
    )


def operations() -> tuple[OpSpec, ...]:
    return (
        _common.build_tenant_read_op(
            op_id="usage.read",
            summary="Show engine-sourced usage for this tenant or one session",
            examples=("show usage for this tenant",),
            params=UsageReadParams,
            result=UsageReadResult,
            handler="arbora.api.ops.usage.handle_usage_read",
            scope="usage:read",
        ),
    )


specs = operations

__all__ = [
    "SessionUsageView",
    "TurnTiming",
    "UsageReadParams",
    "UsageReadResult",
    "UsageReader",
    "UsageSnapshot",
    "handle_usage_read",
    "operations",
    "specs",
]
