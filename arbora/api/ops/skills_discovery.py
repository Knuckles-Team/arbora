"""Skill measures, ranking, peer recommendation and traces (ARBORA-OPS-R954).

Each operation forwards one engine ``SkillTelemetry`` read for the caller's
graph. The engine computes the twelve measures, ranks the skills, derives the
caller's cohort from its own telemetry and lists the run traces by eval
outcome. Arbora ranks nothing and keeps no telemetry. The caller is the
verified principal, so a peer query names no other user.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops._engine import engine_graph_op, unavailable
from arbora.api.registry import OpSpec

TELEMETRY_READ_SCOPE = "telemetry:read"


class WindowParams(Params):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> WindowParams:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must follow start_ms")
        return self


class MeasuresParams(WindowParams):
    skill_ref: str = Field(min_length=1, max_length=512)


class RankParams(WindowParams):
    skill_refs: list[str] = Field(default_factory=list, max_length=200)
    limit: int | None = Field(default=None, ge=1, le=200)


class TracesParams(Params):
    skill_ref: str = Field(min_length=1, max_length=512)
    outcome: Literal["pass", "fail", "unscored"]
    limit: int | None = Field(default=None, ge=1, le=200)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


class SkillMeasures(Params):
    invocations: int = Field(ge=0)
    distinct_users: int = Field(ge=0)
    retained_users_ppm: int = Field(ge=0)
    agent_chosen_ppm: int = Field(ge=0)
    user_chosen_ppm: int = Field(ge=0)
    error_rate_ppm: int = Field(ge=0)
    latency_p50_ms: int | None = None
    latency_p95_ms: int | None = None
    cost_mean_micro_usd: int | None = None
    cost_p95_micro_usd: int | None = None
    eval_pass_rate_ppm: int = Field(ge=0)
    eval_runs: int = Field(ge=0)


class RankedSkill(Params):
    skill_ref: str
    quality_ppm: int = Field(ge=0)
    eval_runs: int = Field(ge=0)
    invocations: int = Field(ge=0)


class RankResult(Params):
    skills: list[RankedSkill]


class PeerSkill(Params):
    skill_ref: str
    cohort_users: int = Field(ge=0)


class PeersResult(Params):
    skills: list[PeerSkill]


class TracesResult(Params):
    trace_ids: list[str]
    next_cursor: str | None = None


def _shaped(model: type[Params], answer: Any) -> dict[str, Any]:
    """The engine answer as the typed result; a different shape is UNAVAILABLE."""
    try:
        return model.model_validate(answer).model_dump(mode="json")
    except ValidationError:
        raise unavailable("engine result malformed") from None


def _window(params: Mapping[str, Any]) -> dict[str, int]:
    return {"start_ms": params["start_ms"], "end_ms": params["end_ms"]}


async def handle_skill_measures(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The twelve measures of one skill over one window."""
    answer = await engine_graph_op(
        context,
        "SkillTelemetry",
        {"op": "measures", "skill_ref": params["skill_ref"], "window": _window(params)},
    )
    return _shaped(SkillMeasures, answer)


async def handle_skill_rank(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Skills ranked by the engine's quality score."""
    request: dict[str, Any] = {
        "op": "rank",
        "skill_refs": list(params["skill_refs"]),
        "window": _window(params),
    }
    if params.get("limit") is not None:
        request["limit"] = params["limit"]
    return _shaped(
        RankResult, await engine_graph_op(context, "SkillTelemetry", request)
    )


async def handle_skill_peers(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The skills the caller's cohort used and the caller did not."""
    answer = await engine_graph_op(
        context, "SkillTelemetry", {"op": "peers", "window": _window(params)}
    )
    return _shaped(PeersResult, answer)


async def handle_skill_traces(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The run traces of one skill with one eval outcome."""
    request: dict[str, Any] = {
        "op": "traces_by_eval",
        "skill_ref": params["skill_ref"],
        "outcome": params["outcome"],
    }
    for key in ("limit", "cursor"):
        if params.get(key) is not None:
            request[key] = params[key]
    return _shaped(
        TracesResult, await engine_graph_op(context, "SkillTelemetry", request)
    )


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="skills.measures.read",
            summary="Read the twelve usage and quality measures of one skill",
            examples=("how is this skill performing",),
            params=MeasuresParams,
            result=SkillMeasures,
            handler="arbora.api.ops.skills_discovery.handle_skill_measures",
            scope=TELEMETRY_READ_SCOPE,
        ),
        build_read_op(
            op_id="skills.rank",
            summary="Rank skills by the engine's quality score over a window",
            examples=("rank these skills by quality",),
            params=RankParams,
            result=RankResult,
            handler="arbora.api.ops.skills_discovery.handle_skill_rank",
            scope=TELEMETRY_READ_SCOPE,
        ),
        build_read_op(
            op_id="skills.peers.recommend",
            summary="Recommend skills the caller's cohort uses and the caller does not",
            examples=("which skills do people like me use",),
            params=WindowParams,
            result=PeersResult,
            handler="arbora.api.ops.skills_discovery.handle_skill_peers",
            scope=TELEMETRY_READ_SCOPE,
        ),
        build_read_op(
            op_id="skills.traces.by_eval",
            summary="List the run traces of a skill by eval outcome",
            examples=("show the failing traces of this skill",),
            params=TracesParams,
            result=TracesResult,
            handler="arbora.api.ops.skills_discovery.handle_skill_traces",
            scope=TELEMETRY_READ_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "handle_skill_measures",
    "handle_skill_peers",
    "handle_skill_rank",
    "handle_skill_traces",
    "operations",
    "specs",
]
