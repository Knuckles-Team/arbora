"""Skill evaluation, ablation and canary served from the engine.

ARBORA-OPS-R955 and R956. Each operation forwards one engine method as the
verified caller. Arbora fills the tenant from the caller, never sends a
reviewer or approver (the engine takes both from the verified caller) and
keeps no eval or rollout state. Engine refusals pass through by code
(``JUDGE_NOT_CALIBRATED``, ``EVAL_SCOPE_REQUIRED``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops._engine import engine_send
from arbora.api.ops.knowledge import EngineReply
from arbora.api.registry import OpSpec, Verb

AUTHOR_SCOPE = "skills:author"
EVAL_SCOPE = "skills:eval"
_Ref = Field(min_length=1, max_length=512)
_Body = Field(description="the engine request body, validated by the engine")
#: Fields only the verified caller may supply.
_CALLER_ONLY = frozenset({"tier_reviewer", "approver_principal", "tenant_id"})
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "skills.eval.case.add": "medulla:DecisionEval",
    "skills.eval.calibrate": "medulla:DecisionEval",
    "skills.ablation.record": "medulla:DecisionLog",
}


class EvalCaseAddParams(Params):
    case: dict[str, Any] = _Body
    known_tools: list[str] = Field(default_factory=list, max_length=512)


class EvalReportParams(Params):
    skill_ref: str = _Ref


class EvalCalibrateParams(Params):
    skill_ref: str = _Ref
    calibration: dict[str, Any] = _Body


class AblationRecordParams(Params):
    record: dict[str, Any] = _Body


class RequestParams(Params):
    request: dict[str, Any] = _Body


def _tenant(context: Any) -> str:
    return str(context.caller.tenant)


async def _eval(
    context: Any, name: str, request: dict[str, Any], *, write: bool
) -> dict[str, Any]:
    body = {"op": {"op": name, "request": {**request, "tenant_id": _tenant(context)}}}
    return {"value": await engine_send(context, "DecisionEval", body, idempotent=write)}


async def handle_case_add(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = {"case": params["case"], "known_tools": list(params["known_tools"])}
    return await _eval(context, "skill_eval_case_add", request, write=True)


async def handle_report(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = {"skill_ref": params["skill_ref"]}
    return await _eval(context, "skill_eval_report", request, write=False)


async def handle_calibrate(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = {"skill_ref": params["skill_ref"], "calibration": params["calibration"]}
    return await _eval(context, "skill_eval_calibrate", request, write=True)


async def handle_ablation_record(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Record one ablation through the engine's learning write."""
    write = {"write": "record_ablation", "record": params["record"]}
    body = {"op": {"op": "learn", "tenant_id": _tenant(context), "write": write}}
    return {"value": await engine_send(context, "DecisionLog", body, idempotent=True)}


def _decide_request(context: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """The caller's request with the caller-only fields taken from the session."""
    request = {
        key: value
        for key, value in params["request"].items()
        if key not in _CALLER_ONLY
    }
    return {**request, "tenant_id": _tenant(context)}


async def decide_as_caller(context: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    body = {"request": _decide_request(context, params)}
    return {"value": await engine_send(context, "Decide", body)}


async def handle_canary_decide(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The engine's canary verdict (ramp, hold or rollback), unchanged."""
    return await decide_as_caller(context, params)


def operations() -> tuple[OpSpec, ...]:
    h = "arbora.api.ops.skills_eval.handle_"
    return (
        build_write_op(
            verb=Verb.WRITE,
            op_id="skills.eval.case.add",
            summary="Add one evaluation case for a skill",
            examples=("add an eval case for this skill",),
            params=EvalCaseAddParams,
            result=EngineReply,
            handler=h + "case_add",
            scope=AUTHOR_SCOPE,
        ),
        build_read_op(
            op_id="skills.eval.report",
            summary="Read a skill's evaluation report and judge calibration",
            examples=("how did this skill do in evaluation",),
            params=EvalReportParams,
            result=EngineReply,
            handler=h + "report",
            scope=EVAL_SCOPE,
        ),
        build_write_op(
            verb=Verb.WRITE,
            op_id="skills.eval.calibrate",
            summary="Record the judge calibration for a skill",
            examples=("calibrate the judge for this skill",),
            params=EvalCalibrateParams,
            result=EngineReply,
            handler=h + "calibrate",
            scope=EVAL_SCOPE,
        ),
        build_write_op(
            verb=Verb.WRITE,
            op_id="skills.ablation.record",
            summary="Record one skill ablation",
            examples=("record this ablation result",),
            params=AblationRecordParams,
            result=EngineReply,
            handler=h + "ablation_record",
            scope=EVAL_SCOPE,
        ),
        build_read_op(
            op_id="skills.canary.decide",
            summary="Ask the engine for a canary verdict: ramp, hold or rollback",
            examples=("should this skill canary ramp",),
            params=RequestParams,
            result=EngineReply,
            handler=h + "canary_decide",
            scope=EVAL_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "decide_as_caller",
    "NATIVE_IDEMPOTENCY",
    "handle_ablation_record",
    "handle_calibrate",
    "handle_canary_decide",
    "handle_case_add",
    "handle_report",
    "operations",
    "specs",
]
