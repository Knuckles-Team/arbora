"""Skill friction reports served from the engine (ARBORA-OPS-R952).

``skills.friction.*`` forwards the engine ``FrictionReport`` method. The engine
overwrites the tenant and the reporter with the verified caller, and Arbora
sends those two from the same verified caller so a body never names them. A
transition into ``approved`` needs ``knowledge:approve`` on the caller; the
engine checks the same grant again. Arbora keeps no friction state.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.engine_calls import engine_graph_op
from arbora.api.ops.knowledge import CURATE_SCOPE, FLAG_SCOPE, EngineReply
from arbora.api.registry import OpSpec, Verb

APPROVE_SCOPE = "knowledge:approve"
FrictionState = Literal[
    "reported", "triaged", "root_caused", "fixed", "evaluated", "approved"
]
_Id = Field(min_length=1, max_length=512)
_Text = Field(min_length=1, max_length=8192)


class FrictionReportParams(Params):
    report_id: str = _Id
    skill_ref: str = _Id
    skill_version: str = _Id
    run_id: str = _Id
    tried: str = _Text
    expected: str = _Text
    happened: str = _Text
    proposed_fix: str | None = Field(default=None, max_length=8192)
    at_ms: int = Field(ge=0)


class FrictionTransitionParams(Params):
    group_ref: str = _Id
    to: FrictionState
    evidence_ref: str | None = Field(default=None, max_length=512)
    at_ms: int = Field(ge=0)


class FrictionGroupParams(Params):
    skill_ref: str | None = Field(default=None, max_length=512)
    limit: int | None = Field(default=None, ge=1, le=500)


class FrictionGetParams(Params):
    group_ref: str = _Id


def _present(params: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}


async def handle_friction_report(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """File one report; the verified caller is the reporter."""
    caller = context.caller
    report = {
        **_present(params),
        "tenant_id": str(caller.tenant),
        "reported_by": str(caller.principal),
    }
    answer = await engine_graph_op(
        context, "FrictionReport", {"op": "report", "report": report}, idempotent=True
    )
    return {"value": answer}


async def handle_friction_transition(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Move one group; approving needs its own grant."""
    if params["to"] == "approved" and APPROVE_SCOPE not in (
        context.caller.effective_scopes
    ):
        raise OperationRefused("POLICY_DENIED", {"missing_scopes": [APPROVE_SCOPE]})
    transition = {
        key: value
        for key, value in _present(params).items()
        if key in {"group_ref", "to", "evidence_ref"}
    }
    answer = await engine_graph_op(
        context,
        "FrictionReport",
        {"op": "transition", "transition": transition, "at_ms": params["at_ms"]},
        idempotent=True,
    )
    return {"value": answer}


async def handle_friction_group(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Friction groups by report count, most first."""
    answer = await engine_graph_op(
        context, "FrictionReport", {"op": "group", **_present(params)}
    )
    return {"value": answer}


async def handle_friction_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One friction group."""
    answer = await engine_graph_op(
        context, "FrictionReport", {"op": "get", "group_ref": params["group_ref"]}
    )
    return {"value": answer}


#: Writes the engine replays by the idempotency key the pipeline forwards.
NATIVE_IDEMPOTENCY: Mapping[str, str] = MappingProxyType(
    {
        "skills.friction.report": "medulla:FrictionReport",
        "skills.friction.transition": "medulla:FrictionReport",
    }
)


def operations() -> tuple[OpSpec, ...]:
    handlers = "arbora.api.ops.skills_friction.handle_friction_"
    return (
        build_write_op(
            verb=Verb.WRITE,
            op_id="skills.friction.report",
            summary="Report what a skill did against what the caller expected",
            examples=("report friction with this skill",),
            params=FrictionReportParams,
            result=EngineReply,
            handler=handlers + "report",
            scope=FLAG_SCOPE,
        ),
        build_write_op(
            verb=Verb.WRITE,
            op_id="skills.friction.transition",
            summary="Move a friction group to its next state",
            examples=("mark this friction group as root caused",),
            params=FrictionTransitionParams,
            result=EngineReply,
            handler=handlers + "transition",
            scope=CURATE_SCOPE,
        ),
        build_read_op(
            op_id="skills.friction.group",
            summary="List friction groups by report count",
            examples=("which skills have the most friction",),
            params=FrictionGroupParams,
            result=EngineReply,
            handler=handlers + "group",
            scope=FLAG_SCOPE,
        ),
        build_read_op(
            op_id="skills.friction.get",
            summary="Read one friction group",
            examples=("show this friction group",),
            params=FrictionGetParams,
            result=EngineReply,
            handler=handlers + "get",
            scope=FLAG_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "handle_friction_get",
    "handle_friction_group",
    "handle_friction_report",
    "handle_friction_transition",
    "operations",
    "specs",
]
