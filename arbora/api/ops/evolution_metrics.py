"""Proposal outcome projection (ARBORA-OPS-R059).

``evolution.proposals.metrics`` joins one evolution proposal to its stored trace
metrics through the engine ``EvolutionTraceMetrics`` method. Medulla owns the
proposal, progress and trace facts; arbora forwards the query unchanged as the
verified caller, returns the engine answer as it came and does no arithmetic. A
pin that predates the method refuses ``UNAVAILABLE`` naming it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops._engine import engine_send
from arbora.api.registry import OpSpec
from arbora.pin_lag.medulla import (
    PinLagUnavailable,
    refusal,
    require_methods,
    require_models,
)

READ_SCOPE = "loops:read"
#: The engine method and request shape of MEDULLA-ORCH-VOCAB-R010.100.
METHOD = "EvolutionTraceMetrics"
MODEL = "EvolutionTraceMetricsQuery"
_ID = Field(min_length=1, max_length=256)
_REV = Field(ge=0)


class EvolutionMetricsQuery(Params):
    """One proposal, its prompt version and its metric window; all identities required."""

    schema_version: Literal[1] = 1
    proposal_id: str = _ID
    proposal_revision: int = _REV
    prompt_version_id: str = _ID
    prompt_revision: int = _REV
    prompt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    metric_window_id: str = _ID
    metric_window_revision: int = _REV
    metric_window_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvolutionMetricsProjection(Params):
    """The engine answer unchanged: trace refs, missing ids, coverage, state and metrics."""

    value: dict[str, Any]


async def handle_evolution_metrics(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Forward the query as the caller; a missing trace stays partial, never zero."""
    try:
        require_models([MODEL])
        require_methods(METHOD)
    except PinLagUnavailable as error:
        raise refusal(error) from error
    answer = await engine_send(context, METHOD, {"request": dict(params)})
    return {
        "value": dict(answer) if isinstance(answer, Mapping) else {"result": answer}
    }


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="evolution.proposals.metrics",
            summary="Join one evolution proposal to its stored trace metrics",
            examples=("show the measured outcome of this proposal",),
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
    "handle_evolution_metrics",
    "operations",
    "specs",
]
