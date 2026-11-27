"""Promote a built artifact through governed operations (ARBORA-DEPLOY-R017.22, .23, .24).

``deploy.promotions.plan`` refuses a tag-only reference and an artifact whose
delivery pipeline did not pass, and returns the step graph the engine assembled.
``deploy.promotions.apply`` runs only after an approver approved the plan at the
engine-declared gate; it runs the target environment steps as governed fleet
calls and writes one deploy record to the engine. A failed step records the
failure and promotes nothing further. Arbora keeps no record store: the engine
holds the deploy record and ``deploy.promotions.record`` reads it back.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.domain.deployment.assembly_view import render_graph
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.plan import approval_id_for
from arbora.api.invoke.steps import OpError
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.deploy import _assemble
from arbora.api.ops.engine_calls import engine_send
from arbora.api.ops.pin_guards import refusal
from arbora.api.registry import Confirm, OpSpec
from arbora.deployment.assembly import (
    graph_digest,
    require_confirmed_plan,
    run_confirmed_steps,
)
from arbora.deployment.image_digests import is_digest_pinned
from arbora.pin_lag.medulla import PinLagUnavailable, require_methods

_DIGEST = r"^sha256:[0-9a-f]{64}$"
_PASSED = frozenset({"passed", "success", "succeeded"})
DEPLOY_RECORD_METHOD = "DeployRecordPut"


class PromotionParams(Params):
    """What is promoted, where from and to, and which steps run."""

    artifact: str = Field(min_length=1, max_length=512)
    source_env: str = Field(min_length=1, max_length=128)
    target_env: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=64)
    components: list[str] = Field(default_factory=list, max_length=64)


class PromotionPlanResult(Params):
    artifact: str
    source_env: str
    target_env: str
    steps: list[dict[str, Any]]
    plan_digest: str
    ci: dict[str, Any]


class PromotionApplyParams(PromotionParams):
    plan_digest: str = Field(pattern=_DIGEST)
    step_arguments: dict[str, dict[str, Any]] = Field(default_factory=dict)


class PromotionApplyResult(Params):
    ok: bool
    steps: list[dict[str, Any]]
    plan_digest: str
    record: dict[str, Any]


class PromotionRecordParams(Params):
    plan_digest: str = Field(pattern=_DIGEST)


class PromotionRecordResult(Params):
    record: dict[str, Any]


def _digest_of(artifact: str) -> str:
    if not is_digest_pinned(artifact):
        raise OperationRefused(
            "IMAGE_NOT_DIGEST_PINNED",
            {"reason": "the artifact must name a sha256 digest"},
        )
    return artifact.rsplit("@", 1)[1]


def _step_invoke(context: Any) -> Any:
    invoke = context.services.get("plan_step_invoke")
    if invoke is None:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the invoke pipeline is not bound"}
        )
    return invoke


async def _require_green_pipeline(context: Any, digest: str) -> dict[str, Any]:
    """Confirm through one governed connector read that the digest's pipeline passed."""
    tool = context.services.get("promotion_ci_tool")
    if not (
        isinstance(tool, Mapping)
        and isinstance(tool.get("server"), str)
        and isinstance(tool.get("tool"), str)
    ):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "no pipeline read is configured"}
        )
    outcome = await _step_invoke(context)(
        "fleet.call",
        {
            "server": tool["server"],
            "tool": tool["tool"],
            "arguments": {"digest": digest},
        },
        context.caller,
        f"promote-ci:{digest}",
    )
    value = None if isinstance(outcome, OpError) else outcome.value
    if isinstance(value, Mapping) and isinstance(value.get("value"), Mapping):
        value = value["value"]
    if not (
        isinstance(value, Mapping)
        and value.get("status") in _PASSED
        and value.get("digest") == digest
    ):
        raise OperationRefused("CI_NOT_GREEN", {"digest": digest})
    return {"status": "passed", "digest": digest}


async def handle_promotion_plan(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    digest = _digest_of(params["artifact"])
    ci = await _require_green_pipeline(context, digest)
    assembly = await _assemble(
        context, str(context.caller.tenant), params["target"], params["components"]
    )
    graph = assembly.get("graph")
    return {
        "artifact": params["artifact"],
        "source_env": params["source_env"],
        "target_env": params["target_env"],
        "steps": render_graph(graph),
        "plan_digest": graph_digest(graph),
        "ci": ci,
    }


def _now_ms(context: Any) -> int:
    clock = context.services.get("now_ms")
    return int(clock()) if callable(clock) else int(time.time() * 1000)


async def _require_approved(context: Any) -> str:
    """The approval item of this plan must be approved; return its id."""
    key = context.idempotency_key or ""
    if not key.startswith("plan:"):
        raise OperationRefused("APPROVAL_REQUIRED", {})
    approval_id = approval_id_for(key.removeprefix("plan:"))
    item = await context.client.control_leases.get(
        tenant=context.caller.tenant, lease_id=approval_id
    )
    if not (isinstance(item, Mapping) and item.get("status") == "consumed"):
        raise OperationRefused("APPROVAL_REQUIRED", {})
    return approval_id


def _record_body(
    params: Mapping[str, Any],
    rows: list[dict[str, Any]],
    run: Mapping[str, Any],
    approval_id: str,
    started: int,
    finished: int,
) -> dict[str, Any]:
    done = {step["node_id"]: step for step in run["steps"]}
    nodes: list[dict[str, Any]] = []
    failed = False
    for row in rows:
        step = done.get(row["node_id"])
        if step is None:
            nodes.append({"node": row["node_id"], "outcome": "skipped"})
        elif step["ok"]:
            nodes.append({"node": row["node_id"], "outcome": "succeeded"})
        else:
            failed = True
            nodes.append(
                {
                    "node": row["node_id"],
                    "outcome": "failed",
                    "code": step.get("error", "FAILED"),
                }
            )
    assert failed or run["ok"]
    return {
        "plan_digest": params["plan_digest"],
        "target": params["target_env"],
        "nodes": nodes,
        "image_digests": [_digest_of(params["artifact"])],
        "approver": approval_id,
        "started_at_ms": started,
        "finished_at_ms": finished,
    }


async def handle_promotion_apply(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    require_confirmed_plan(op)
    _digest_of(params["artifact"])
    try:
        # Nothing runs that the engine cannot record.
        require_methods(DEPLOY_RECORD_METHOD)
    except PinLagUnavailable as missing:
        raise refusal(missing) from missing
    approval_id = await _require_approved(context)
    assembly = await _assemble(
        context, str(context.caller.tenant), params["target"], params["components"]
    )
    graph = assembly.get("graph")
    started = _now_ms(context)
    run = await run_confirmed_steps(
        _step_invoke(context),
        context.caller,
        graph,
        plan_digest=params["plan_digest"],
        arguments=params["step_arguments"],
    )
    body = _record_body(
        params, render_graph(graph), run, approval_id, started, _now_ms(context)
    )
    receipt = await engine_send(
        context, DEPLOY_RECORD_METHOD, {"record": body}, idempotent=True
    )
    return {
        "ok": run["ok"],
        "steps": run["steps"],
        "plan_digest": params["plan_digest"],
        "record": dict(receipt) if isinstance(receipt, Mapping) else {},
    }


async def handle_promotion_record(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    try:
        require_methods("DeployRecordGet")
    except PinLagUnavailable as missing:
        raise refusal(missing) from missing
    answer = await engine_send(
        context, "DeployRecordGet", {"plan_digest": params["plan_digest"]}
    )
    body = answer.get("record") if isinstance(answer, Mapping) else None
    return {"record": dict(body) if isinstance(body, Mapping) else {}}


NATIVE_IDEMPOTENCY: dict[str, str] = {
    "deploy.promotions.apply": "arbora.api.domain.mcp_server.effect_journal:EgEffectJournal",
}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="deploy.promotions.plan",
            summary="Plan promoting a digest-pinned artifact whose pipeline passed",
            examples=("promote this image to staging",),
            params=PromotionParams,
            result=PromotionPlanResult,
            handler="arbora.api.ops.promote.handle_promotion_plan",
            scope="ops:read",
        ),
        build_write_op(
            op_id="deploy.promotions.apply",
            summary="Run an approved promotion and record the deployment",
            examples=("apply the approved promotion",),
            params=PromotionApplyParams,
            result=PromotionApplyResult,
            handler="arbora.api.ops.promote.handle_promotion_apply",
            scope="fleet:control",
            confirm=Confirm.PLAN,
        ),
        build_read_op(
            op_id="deploy.promotions.record",
            summary="Read the deploy record of one promotion",
            examples=("show the deploy record for this promotion",),
            params=PromotionRecordParams,
            result=PromotionRecordResult,
            handler="arbora.api.ops.promote.handle_promotion_record",
            scope="ops:read",
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "handle_promotion_apply",
    "handle_promotion_plan",
    "handle_promotion_record",
    "operations",
    "specs",
]
