"""Capacity questions the engine answers: ingest shortfall and run adjustment.

``capacity.ingest_shortfall.get`` (ARBORA-CAPACITY-R003) measures the paper-ingest window
with ``MeasureIngestThroughput`` and asks ``Decide`` for the ``ingest_shortfall``
question with that window, the caller's goals and budget and the optional purge
candidates. The engine owns the target and the below-target policy and answers
advisory options ``goal:<id>`` (a goal it funds) and ``purge:<id>`` (a purge it
allows). Arbora derives each action's method, cost and revision from the goal it
funded; the dispatch of a funded action is that action's own served operation.

``capacity.run_adjustment.get`` (ARBORA-CAPACITY-R001.2) asks ``Decide`` for the
``run_adjustment`` question of one run. The sealed decision record is the joint
adjustment receipt: before and after model, width and budget, with the rationale. The
run window belongs to meristem, which writes it to the graph and applies the record
after it commits. A mid-run widening is the engine's own ``WIDEN_MID_RUN`` refusal, so
the host ceiling and narrow-only stay engine-enforced and nothing here applies a change.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.ops._engine import engine_send
from arbora.api.ops._question import (
    FamilyRefusal,
    advisory_scores,
    decide_question,
    question_request,
)
from arbora.api.registry import OpSpec

SCOPE = "capacity:read"
SHORTFALL_QUESTION = "ingest_shortfall"
SHORTFALL_REFUSALS = frozenset({"WINDOW_UNMEASURED", "BUDGET_INVALID", "GOALS_EMPTY"})
ADJUSTMENT_QUESTION = "run_adjustment"
ADJUSTMENT_REFUSALS = frozenset(
    {
        "WIDEN_MID_RUN",
        "ERRORS_UNPARTITIONED",
        "WINDOW_STALE",
        "RATE_UNKNOWN",
        "INSUFFICIENT_SAMPLES",
    }
)


class IngestShortfallParams(_common.Params):
    """The window's corpus and end, the goals to fund and the budget they share."""

    corpus: str = Field(min_length=1, max_length=256)
    window_end_ms: int = Field(ge=0)
    goals: list[dict[str, Any]] = Field(min_length=1, max_length=64)
    budget: dict[str, Any]
    purge_candidates: list[dict[str, Any]] = Field(default_factory=list, max_length=256)


class RunAdjustmentParams(_common.Params):
    run_id: str = Field(min_length=1, max_length=256)


class CapacityAnswerResult(_common.Params):
    value: dict[str, Any]


def _by_family(options: tuple[tuple[str, int], ...]) -> dict[str, list[str]]:
    """The advisory options by family: funded ``goal:`` ids and allowed ``purge:`` ids."""
    names = [name for name, _ in options]
    return {
        "goals": [n.removeprefix("goal:") for n in names if n.startswith("goal:")],
        "purge": [n.removeprefix("purge:") for n in names if n.startswith("purge:")],
    }


def _text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


async def _ask(
    context: Any,
    kind: str,
    question_id: str,
    options: list[Any],
    params: list[Any],
    family_codes: frozenset[str],
) -> Mapping[str, Any]:
    from medulla import decision_client as dc

    from arbora.api.ops.plans import _feature_schema

    tenant = str(context.caller.tenant)
    request = question_request(
        tenant,
        question_id,
        kind,
        dc.declared([dc.DeclaredOption(name) for name in options]),
        await _feature_schema(context, tenant),
        params,
    )
    try:
        return await decide_question(context, kind, request, family_codes=family_codes)
    except FamilyRefusal as refused:
        raise OperationRefused(
            "POLICY_DENIED", {"engine_code": refused.code}
        ) from refused


async def handle_ingest_shortfall_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Measure the window, ask the engine, return what it funds and allows to purge."""
    from medulla import decision_client as dc

    window = await engine_send(
        context,
        "MeasureIngestThroughput",
        {"corpus": params["corpus"], "window_end_ms": params["window_end_ms"]},
    )
    typed = [
        dc.param("window", "text", _text(window)),
        dc.param("goals", "text", _text(params["goals"])),
        dc.param("budget", "text", _text(params["budget"])),
    ]
    if params["purge_candidates"]:
        typed.append(
            dc.param("purge_candidates", "text", _text(params["purge_candidates"]))
        )
    record = await _ask(
        context,
        SHORTFALL_QUESTION,
        f"ingest.shortfall:{params['corpus']}",
        [f"goal:{goal['goal_id']}" for goal in params["goals"]],
        typed,
        SHORTFALL_REFUSALS,
    )
    return {"value": {"window": window, **_by_family(advisory_scores(record))}}


async def handle_run_adjustment_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The engine's adjustment record of one run; the record is the receipt."""
    from medulla import decision_client as dc

    record = await _ask(
        context,
        ADJUSTMENT_QUESTION,
        f"run.adjustment:{params['run_id']}",
        ["adjustment"],
        [dc.param("run_id", "text", params["run_id"])],
        ADJUSTMENT_REFUSALS,
    )
    return {"value": {"run_id": params["run_id"], "record": dict(record)}}


def operations() -> tuple[OpSpec, ...]:
    return (
        _common.build_read_op(
            op_id="capacity.ingest_shortfall.get",
            summary="Show which goals and purges the engine funds for the ingest shortfall",
            examples=("how should the paper ingest shortfall be funded",),
            params=IngestShortfallParams,
            result=CapacityAnswerResult,
            handler="arbora.api.ops.capacity_questions.handle_ingest_shortfall_get",
            scope=SCOPE,
        ),
        _common.build_read_op(
            op_id="capacity.run_adjustment.get",
            summary="Show the engine's model and width adjustment record of one run",
            examples=("what adjustment did the engine decide for this run",),
            params=RunAdjustmentParams,
            result=CapacityAnswerResult,
            handler="arbora.api.ops.capacity_questions.handle_run_adjustment_get",
            scope=SCOPE,
        ),
    )


__all__ = [
    "handle_ingest_shortfall_get",
    "handle_run_adjustment_get",
    "operations",
]
