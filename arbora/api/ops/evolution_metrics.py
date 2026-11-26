"""Technique outcome projection (ARBORA-OPS-R059).

``evolution.proposals.metrics`` measures one technique under one arm and one
configuration: the latency, cost and score of the three together are three
engine ``RunMetrics`` queries over the same ``technique``, ``arm`` and
``config_digest`` filter (MEDULLA-ORCH-VOCAB-R811.1.1). Medulla owns the run
traces and does the aggregation; arbora forwards each query as the verified
caller and returns the answers as they came. A population that matches nothing
or reports no such counter stays abstained, never zero. A pin that predates the
method refuses ``UNAVAILABLE`` naming it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field, model_validator

from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops.usage import run_metrics_answer
from arbora.api.registry import OpSpec

READ_SCOPE = "loops:read"
#: The three queries of one technique measure, in the order they are answered.
MEASURES: tuple[str, ...] = ("latency", "cost", "score")
_ID = Field(min_length=1, max_length=256)


class EvolutionMetricsQuery(Params):
    """One technique measure: the technique, its arm and the configuration it ran under."""

    schema_version: int = Field(default=1, ge=1, le=1)
    technique: str = _ID
    arm: str = _ID
    config_digest: str = _ID
    window_start_ms: int | None = Field(default=None, ge=0)
    window_end_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _window_is_a_nonempty_pair(self) -> EvolutionMetricsQuery:
        start, end = self.window_start_ms, self.window_end_ms
        if (start is None) != (end is None):
            raise ValueError("a window names both bounds or neither")
        if start is not None and end is not None and end <= start:
            raise ValueError("window_end_ms must follow window_start_ms")
        return self


class EvolutionMetricsProjection(Params):
    """The filter and one engine answer per measure, each carried unchanged."""

    value: dict[str, Any]


async def handle_evolution_metrics(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Ask the engine the three measures of one technique; an abstention stays one."""
    measures: dict[str, Any] = {}
    for query in MEASURES:
        measures[query] = await run_metrics_answer(context, {**params, "query": query})
    return {
        "value": {
            "technique": params["technique"],
            "arm": params["arm"],
            "config_digest": params["config_digest"],
            "measures": measures,
        }
    }


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="evolution.proposals.metrics",
            summary="Measure the latency, cost and score of one technique arm",
            examples=("show the measured outcome of this technique arm",),
            params=EvolutionMetricsQuery,
            result=EvolutionMetricsProjection,
            handler="arbora.api.ops.evolution_metrics.handle_evolution_metrics",
            scope=READ_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "EvolutionMetricsProjection",
    "EvolutionMetricsQuery",
    "MEASURES",
    "handle_evolution_metrics",
    "operations",
    "specs",
]
