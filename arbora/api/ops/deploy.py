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
from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops._engine import engine_send, refusal_code
from arbora.api.registry import OpSpec
from arbora.deployment.assembly import assemble_deployment, refuse_unavailable
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

    async def assemble(body: dict[str, Any]) -> Any:
        return await engine_send(context, "AgentAssemble", body)

    assembly = await assemble_deployment(
        assemble, tenant=tenant, target=target, components=params["components"]
    )
    return {
        "decision": dict(record),
        "target": target,
        "assembly": dict(assembly),
        "steps": render_graph(assembly.get("graph")),
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
    )


specs = operations

__all__ = [
    "PlacementPlanParams",
    "PlacementPlanResult",
    "handle_placement_plan",
    "operations",
    "specs",
]
