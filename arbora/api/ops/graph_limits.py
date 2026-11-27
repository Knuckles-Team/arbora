"""Owner-published live graph limits (ARBORA-OPS-R062.6).

The engine reads its own configuration and answers the commit-to-visible,
frame and layout limits with their profile and contract revisions and the
workload they were measured under. Arbora forwards them after checking that
each limit is positive and finite; it defaults nothing and stores nothing.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from pydantic import Field, ValidationError, field_validator

from arbora.api.ops import _common
from arbora.api.ops.engine_calls import engine_send, unavailable
from arbora.api.registry import OpSpec

#: The engine method that publishes the limits (MEDULLA FOR ARBORA, see the gaps ledger).
LIMITS_METHOD = "GraphLiveLimits"


class GraphLimitsParams(_common.Params):
    pass


class GraphLiveLimits(_common.Params):
    profile_revision: int = Field(gt=0, strict=True)
    contract_revision: str = Field(min_length=1, max_length=128)
    workload: str = Field(min_length=1, max_length=512)
    commit_to_visible_p95_ms: float = Field(gt=0)
    frame_p95_ms: float = Field(gt=0)
    layout_settle_ms: float = Field(gt=0)

    @field_validator(
        "commit_to_visible_p95_ms", "frame_p95_ms", "layout_settle_ms", mode="before"
    )
    @classmethod
    def _finite_number(cls, value: Any) -> float:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("limit must be a finite number")
        return float(value)


class GraphLimitsResult(_common.Params):
    value: GraphLiveLimits


async def handle_graph_limits_read(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Forward the engine's live graph limits, or refuse as unavailable."""
    answer = await engine_send(context, LIMITS_METHOD, {})
    try:
        limits = GraphLiveLimits.model_validate(answer)
    except ValidationError:
        raise unavailable("engine live graph limits are missing or invalid") from None
    return {"value": limits.model_dump(mode="json")}


def operations() -> tuple[OpSpec, ...]:
    return (
        _common.build_tenant_read_op(
            op_id="graph.limits.read",
            summary="Show the owner's live graph latency and layout limits",
            examples=("show the live graph limits",),
            params=GraphLimitsParams,
            result=GraphLimitsResult,
            handler="arbora.api.ops.graph_limits.handle_graph_limits_read",
            scope="kg:read",
        ),
    )


specs = operations

__all__ = [
    "GraphLimitsParams",
    "GraphLimitsResult",
    "GraphLiveLimits",
    "handle_graph_limits_read",
    "operations",
    "specs",
]
