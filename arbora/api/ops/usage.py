"""Usage read operation (ARBORA-OPS-R024.3).

``usage.read`` sources its numbers from the engine, not a counter Arbora keeps
on the side. With a ``context_id`` it forwards the engine ``GetSessionUsage``
read for the caller's own tenant (ARBORA-OPS-R024.3.1) and returns the engine's
counts, coverage and per-turn timings unchanged (ARBORA-OPS-R024.3.2). Without
one it reads the tenant's engine usage ledger rollup for a time window
(ARBORA-OPS-R024.3); Arbora keeps no counter and does no arithmetic.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.ops._engine import engine_send, unavailable
from arbora.api.registry import OpSpec

#: The longest elapsed time one turn timing may report, in milliseconds.
MAX_ELAPSED_MS = 86_400_000


class UsageReadParams(_common.Params):
    context_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$", max_length=128
    )
    from_ms: int | None = Field(default=None, ge=0)
    to_ms: int | None = Field(default=None, ge=0)
    group_by: Literal["model", "project", "agent"] | None = None


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


class UsageGroup(_common.Params):
    """One engine rollup group; an unpriced or mixed-currency group has no cost."""

    key: str
    calls: int = Field(ge=0)
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    tokens_cached: int = Field(ge=0)
    errors: int = Field(ge=0)
    cost_micro: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    latency_p95_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _cost_has_a_currency(self) -> UsageGroup:
        if (self.cost_micro is None) != (self.currency is None):
            raise ValueError("cost and currency are present together or not at all")
        return self


class UsageReadResult(_common.Params):
    value: dict[str, Any] | None = None
    session: SessionUsageView | None = None


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


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
    return {"value": await read_tenant_rollup(context, params)}


async def read_tenant_rollup(
    context: Any,
    params: Mapping[str, Any],
    *,
    clock_ms: Callable[[], int] = _now_ms,
) -> dict[str, Any]:
    """The engine ``Usage`` rollup for the caller's tenant over one window.

    The window defaults to everything up to now; the engine's own per-day,
    per-model rows return unchanged and an engine refusal keeps its code.
    """
    group_by = params.get("group_by")
    if group_by is not None and not _declares_usage_groups():
        raise unavailable("the installed engine does not group usage")
    start = params.get("from_ms") or 0
    end = params.get("to_ms")
    end = clock_ms() if end is None else end
    if end < start:
        raise OperationRefused("INVALID_ARGUMENT", {"reason": "to_ms before from_ms"})
    read: dict[str, Any] = {
        "op": "read",
        "view": "rollup",
        "from_ms": start,
        "to_ms": end,
    }
    if group_by is not None:
        read["group_by"] = group_by
    answer = await engine_send(context, "Usage", {"op": read})
    if not isinstance(answer, Mapping) or answer.get("view") != "rollup":
        raise unavailable("engine result malformed")
    return _checked_rollup(dict(answer), grouped=group_by is not None)


def _declares_usage_groups() -> bool:
    """Whether the installed engine contract declares the grouped rollup."""
    from medulla.generated import models

    return hasattr(models, "UsageGroupRollup")


def _checked_rollup(answer: dict[str, Any], *, grouped: bool) -> dict[str, Any]:
    """The rollup with its groups and watermark validated; nothing is derived."""
    watermark = answer.get("index_watermark_ms")
    if watermark is not None and (type(watermark) is not int or watermark < 0):
        raise unavailable("engine result malformed")
    if grouped and ("groups" not in answer or "index_watermark_ms" not in answer):
        raise unavailable("engine result malformed")
    if "groups" in answer:
        try:
            answer["groups"] = [
                UsageGroup.model_validate(row).model_dump(mode="json")
                for row in answer["groups"]
            ]
        except (ValidationError, TypeError):
            raise unavailable("engine result malformed") from None
    return answer


_FILTER = Field(default=None, min_length=1, max_length=256)


class UsageIndexQuery(_common.Params):
    """The engine ``RunMetrics`` query over the run traces of the caller's tenant.

    ``query`` is ``cost`` or ``tokens`` (per solved run), ``latency`` or
    ``score`` (the mean of the solved runs that report one) or ``solved``. The
    ``technique``, ``arm`` and ``config_digest`` filters together select one
    technique measure. The window is both bounds or neither.
    """

    schema_version: Literal[1] = 1
    query: Literal["cost", "tokens", "latency", "score", "solved"]
    group_by: (
        Literal["agent", "project", "termination", "technique", "arm", "config"] | None
    ) = None
    window_start_ms: int | None = Field(default=None, ge=0)
    window_end_ms: int | None = Field(default=None, ge=0)
    technique: str | None = _FILTER
    arm: str | None = _FILTER
    config_digest: str | None = _FILTER

    @model_validator(mode="after")
    def _window_is_a_nonempty_pair(self) -> UsageIndexQuery:
        start, end = self.window_start_ms, self.window_end_ms
        if (start is None) != (end is None):
            raise ValueError("a window names both bounds or neither")
        if start is not None and end is not None and end <= start:
            raise ValueError("window_end_ms must follow window_start_ms")
        return self


class UsageIndexProjection(_common.Params):
    """The engine answer unchanged: ``query``, ``groups`` and ``abstained``."""

    value: dict[str, Any]


#: The engine method of the run-trace measure (MEDULLA-ORCH-VOCAB-R811.1, R811.1.1).
INDEX_METHOD = "RunMetrics"


def run_metrics_request(context: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """The engine ``RunMetrics`` request: the verified tenant plus the named filters."""
    request: dict[str, Any] = {
        "tenant": str(context.caller.tenant),
        "query": params["query"],
    }
    if params.get("group_by") is not None:
        request["group_by"] = params["group_by"]
    if params.get("window_start_ms") is not None:
        request["window"] = {
            "start_ms": params["window_start_ms"],
            "end_ms": params["window_end_ms"],
        }
    for name in ("technique", "arm", "config_digest"):
        if params.get(name) is not None:
            request[name] = params[name]
    return request


async def run_metrics_answer(context: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Send ``RunMetrics`` as the verified caller; a pin without it refuses ``UNAVAILABLE``."""
    from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_methods

    try:
        require_methods(INDEX_METHOD)
    except PinLagUnavailable as error:
        raise refusal(error) from error
    answer = await engine_send(
        context, INDEX_METHOD, {"request": run_metrics_request(context, params)}
    )
    if (
        not isinstance(answer, Mapping)
        or not isinstance(answer.get("groups"), list)
        or not isinstance(answer.get("abstained"), (str, type(None)))
    ):
        raise unavailable("engine result malformed")
    return dict(answer)


async def handle_usage_index_query(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Forward the measure as the verified caller; arbora does no arithmetic."""
    return {"value": await run_metrics_answer(context, params)}


def operations() -> tuple[OpSpec, ...]:
    return (
        _common.build_tenant_read_op(
            op_id="usage.index.query",
            summary="Measure cost, tokens, latency or score over the run traces",
            examples=("what did runs cost by technique last week",),
            params=UsageIndexQuery,
            result=UsageIndexProjection,
            handler="arbora.api.ops.usage.handle_usage_index_query",
            scope="usage:read",
        ),
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
    "UsageIndexProjection",
    "UsageIndexQuery",
    "handle_usage_index_query",
    "run_metrics_answer",
    "run_metrics_request",
    "SessionUsageView",
    "TurnTiming",
    "UsageGroup",
    "UsageReadParams",
    "UsageReadResult",
    "read_tenant_rollup",
    "handle_usage_read",
    "operations",
    "specs",
]
