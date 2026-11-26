"""Deployment placement and assembly through the engine (ARBORA-DEPLOY-R017.19).

``deploy.placement.plan`` asks the engine where a workload runs (one typed
``Decide`` question) and has it assemble the ordered fleet-tool steps as an
agent graph. Arbora keeps no planner: the placement record and the graph come
back as the engine sent them, with the graph also listed read only for the
operator to confirm. Nothing runs from this operation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.bootstrap import caller_dispatch_graph
from arbora.api.invoke.steps import claim_values
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops._engine import engine_send, refusal_code
from arbora.api.registry import Confirm, OpSpec
from arbora.deployment.assembly import (
    assemble_deployment,
    choices_digest,
    graph_digest,
    refuse_unavailable,
    require_confirmed_plan,
    run_confirmed_steps,
)
from arbora.deployment.assembly_view import render_graph
from arbora.deployment.placement import ask_placement, chosen_target
from arbora.security.anonymous import client_scope

#: Engine refusal tokens that mean the question kind is not served.
_KIND_UNSERVED = frozenset(
    {"UNSUPPORTED_QUESTION_KIND", "UNKNOWN_QUESTION_KIND", "QUESTION_KIND_UNAVAILABLE"}
)


class PlacementPlanParams(Params):
    workload_iri: str = Field(min_length=1, max_length=2048)
    cpu_millis: int = Field(ge=0)
    memory_bytes: int = Field(ge=0)
    replicas: int = Field(ge=0)
    needs_persistent_volume: bool = False
    requires_ha: bool = False
    forbidden_targets: list[str] = Field(default_factory=list, max_length=16)
    max_relative_cost: int | None = Field(default=None, ge=0)
    capacity_cells: list[str] = Field(default_factory=list, max_length=64)
    priority: Literal[
        "interactive", "orchestration", "hydration", "background_ingestion"
    ] = "orchestration"
    candidate_targets: list[str] = Field(min_length=1, max_length=16)
    components: list[str] = Field(default_factory=list, max_length=64)


class PlacementPlanResult(Params):
    decision: dict[str, Any]
    target: str
    assembly: dict[str, Any]
    steps: list[dict[str, Any]]
    plan_digest: str
    choices_digest: str


_DIGEST = r"^sha256:[0-9a-f]{64}$"


class PlacementApplyParams(Params):
    """The confirmed deployment: the values the plan reply showed, bound by the plan."""

    target: str = Field(min_length=1, max_length=64)
    components: list[str] = Field(default_factory=list, max_length=64)
    reuse_components: list[str] = Field(default_factory=list, max_length=64)
    step_arguments: dict[str, dict[str, Any]] = Field(default_factory=dict)
    plan_digest: str = Field(pattern=_DIGEST)
    choices_digest: str = Field(pattern=_DIGEST)
    config_digest: str = Field(pattern=_DIGEST)
    artifact_digest: str = Field(pattern=_DIGEST)


class PlacementApplyResult(Params):
    ok: bool
    steps: list[dict[str, Any]]
    plan_digest: str
    config_digest: str
    artifact_digest: str


def _feature_schema(context: Any, tenant: str) -> Mapping[str, Any]:
    """The tenant's published feature-schema pin that every ``Decide`` names."""
    fixed = context.services.get("deploy_feature_schema")
    if isinstance(fixed, Mapping):
        return fixed
    from arbora.api.task_plan_schema import published_pin

    return published_pin(context.services.get("task_plan_schema_pins"), tenant)


async def handle_placement_plan(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The engine's placement record, its assembled graph and the read-only steps."""
    from medulla import EngineResponseError
    from medulla.decision_client import DecisionClient
    from medulla.generated import METHOD_IDS

    if "Decide" not in METHOD_IDS:
        raise refuse_unavailable("decide")
    tenant = str(context.caller.tenant)
    graph = await caller_dispatch_graph(context)

    async def decide(request: dict[str, Any]) -> Any:
        with client_scope(context.client, claim_values(context.caller.engine_claims)):
            return await DecisionClient(context.client, graph).decide(request)

    try:
        record = await ask_placement(
            decide,
            tenant=tenant,
            principal=str(context.caller.principal),
            graph=graph,
            feature_schema=_feature_schema(context, tenant),
            subject={
                "workload_iri": params["workload_iri"],
                "cpu_millis": params["cpu_millis"],
                "memory_bytes": params["memory_bytes"],
                "replicas": params["replicas"],
                "needs_persistent_volume": params["needs_persistent_volume"],
            },
            constraints={
                "requires_ha": params["requires_ha"],
                "forbidden_targets": list(params["forbidden_targets"]),
                "max_relative_cost": params["max_relative_cost"],
                "capacity_cells": list(params["capacity_cells"]),
                "priority": params["priority"],
            },
            candidate_targets=params["candidate_targets"],
        )
    except EngineResponseError as exc:
        code = refusal_code(exc.code, exc.detail)
        if code in _KIND_UNSERVED:
            raise refuse_unavailable("placement_kind") from exc
        raise EngineRefusal(code) from exc
    target = chosen_target(record)

    assembly = await _assemble(context, tenant, target, params["components"])
    return {
        "decision": dict(record),
        "target": target,
        "assembly": dict(assembly),
        "steps": render_graph(assembly.get("graph")),
        "plan_digest": graph_digest(assembly.get("graph")),
        "choices_digest": choices_digest(target, params["components"], ()),
    }


async def _assemble(
    context: Any, tenant: str, target: str, components: list[str]
) -> Mapping[str, Any]:
    async def assemble(body: dict[str, Any]) -> Any:
        return await engine_send(context, "AgentAssemble", body)

    return await assemble_deployment(
        assemble, tenant=tenant, target=target, components=components
    )


async def handle_placement_apply(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Run the confirmed graph; the pipeline consumed the plan before this call.

    The engine assembles again (evaluate only) and the fresh graph must hash to
    the confirmed ``plan_digest``, so a changed library or a changed choice runs
    nothing.
    """
    from arbora.api.invoke.pipeline import OperationRefused

    require_confirmed_plan(op)
    target, components = params["target"], list(params["components"])
    reuse = list(params["reuse_components"])
    if choices_digest(target, components, reuse) != params["choices_digest"]:
        raise OperationRefused("PLAN_MISMATCH", {"reason": "the choices changed"})
    assembly = await _assemble(context, str(context.caller.tenant), target, components)
    graph = assembly.get("graph")
    if not isinstance(graph, Mapping):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the engine assembled no step graph"}
        )
    invoke = context.services.get("plan_step_invoke")
    if invoke is None:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the invoke pipeline is not bound"}
        )
    run = await run_confirmed_steps(
        invoke,
        context.caller,
        graph,
        plan_digest=params["plan_digest"],
        reuse=reuse,
        arguments=params["step_arguments"],
    )
    return {
        **run,
        "plan_digest": params["plan_digest"],
        "config_digest": params["config_digest"],
        "artifact_digest": params["artifact_digest"],
    }


#: A replay of a confirmed apply returns the recorded node outcomes: every node
#: is one ``fleet.call`` reserved in the engine effect journal under
#: ``deploy:<plan_digest>:<node_id>`` (ARBORA-DEPLOY-R017.21).
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "deploy.placement.apply": "arbora.mcp_server.effect_journal:EgEffectJournal",
}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="deploy.placement.plan",
            summary="Ask the engine where a workload runs and what steps deploy it",
            examples=("where should this workload run",),
            params=PlacementPlanParams,
            result=PlacementPlanResult,
            handler="arbora.api.ops.deploy.handle_placement_plan",
            scope="ops:read",
        ),
        build_write_op(
            op_id="deploy.placement.apply",
            summary="Run the confirmed deployment steps through governed fleet calls",
            examples=("apply the planned deployment",),
            params=PlacementApplyParams,
            result=PlacementApplyResult,
            handler="arbora.api.ops.deploy.handle_placement_apply",
            scope="fleet:control",
            confirm=Confirm.PLAN,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "PlacementApplyParams",
    "PlacementApplyResult",
    "PlacementPlanParams",
    "PlacementPlanResult",
    "handle_placement_apply",
    "handle_placement_plan",
    "operations",
    "specs",
]
