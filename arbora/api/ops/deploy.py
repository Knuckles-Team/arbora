"""Deployment placement and assembly through the engine (ARBORA-DEPLOY-R017.19).

``deploy.placement.plan`` asks the engine where a workload runs (one typed
``Decide`` question) and has it assemble the ordered fleet-tool steps as an
agent graph. Arbora keeps no planner: the placement record and the graph come
back as the engine sent them, with the graph also listed read only for the
operator to confirm. Nothing runs from this operation.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.bootstrap import caller_dispatch_graph
from arbora.api.invoke.steps import claim_values
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.engine_calls import engine_send, refusal_code
from arbora.api.registry import Confirm, Effect, OpSpec
from arbora.deployment.assembly import (
    artifact_digest,
    assemble_deployment,
    choices_digest,
    config_digest,
    graph_digest,
    propagate_trust_step,
    refuse_unavailable,
    require_confirmed_plan,
    require_fresh_binding,
    run_confirmed_steps,
    store_secret_step,
)
from arbora.deployment.assembly_view import render_graph
from arbora.deployment.manifest import (
    DeploymentDiff,
    DeploymentManifest,
    ManifestRefused,
    apply_manifest,
    load_manifest,
    missing_inputs,
    plan_manifest,
    record_rollout,
)
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
    config_digest: str
    artifact_digest: str


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
    stored_secrets: list[str] = Field(default_factory=list)
    trust: list[dict[str, Any]] = Field(default_factory=list)
    plan_digest: str
    config_digest: str
    artifact_digest: str


def feature_schema_of(context: Any, tenant: str) -> Mapping[str, Any]:
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
            feature_schema=feature_schema_of(context, tenant),
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
        "config_digest": config_digest(deploy_config_of(context)),
        "artifact_digest": _current_artifact_digest(context, target),
    }


def _current_artifact_digest(context: Any, target: str) -> str:
    """The digest of the shipped artifact the target deploys; empty when none ships."""
    from pathlib import Path

    from arbora.deployment.artifacts import ArtifactRefused, select_artifact

    root = context.services.get("deploy_root")
    try:
        return artifact_digest(
            select_artifact(target, deploy_root=Path(root) if root else None)
        )
    except ArtifactRefused:
        return ""


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
    if invoke is None or not str(context.caller.principal):
        raise OperationRefused(
            "AUTHORITY_UNAVAILABLE",
            {"reason": "the checked actor or pipeline is not bound"},
        )
    root = context.services.get("deploy_root")
    require_fresh_binding(
        target=target,
        expected_config=params["config_digest"],
        expected_artifact=params["artifact_digest"],
        config=deploy_config_of(context),
        deploy_root=Path(root) if root else None,
    )
    # Secrets default to the vault, before any node runs (ARBORA-DEPLOY-R013.7).
    stored = await store_secret_step(
        invoke,
        context.caller,
        deploy_config_of(context),
        plan_digest=params["plan_digest"],
    )
    run = await run_confirmed_steps(
        invoke,
        context.caller,
        graph,
        plan_digest=params["plan_digest"],
        reuse=reuse,
        arguments=params["step_arguments"],
    )
    if run["ok"]:
        # The trust bundle reaches every component, each verified over TLS.
        run = {
            **run,
            **stored,
            **await propagate_trust_step(
                invoke,
                context.caller,
                deploy_config_of(context),
                plan_digest=params["plan_digest"],
                components=components,
            ),
        }
    return {
        **run,
        "plan_digest": params["plan_digest"],
        "config_digest": params["config_digest"],
        "artifact_digest": params["artifact_digest"],
    }


class OntologyImportParams(Params):
    """The schema source candidate and the SHA-256 the operator confirmed."""

    connector: str = Field(min_length=1, max_length=256)
    ontology_iri: str = Field(min_length=1, max_length=2048)
    version: str = Field(min_length=1, max_length=64)
    ontology_ttl: str = Field(min_length=1, max_length=4_000_000)
    shapes_ttl: str | None = Field(default=None, max_length=4_000_000)
    sha256: str = Field(pattern=_DIGEST)
    expected_prior_revision: int | None = Field(default=None, ge=0)


class OntologyImportResult(Params):
    ontology_iri: str
    sha256: str
    served_digest: str
    revision: int | None
    changed: bool


def _sha256(text: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _existing_ontology(
    context: Any, params: Mapping[str, Any]
) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
    """The served source listing and the source already serving this ontology."""
    from arbora.api.invoke.pipeline import OperationRefused

    listing = await engine_send(context, "GraphSchemaList", {})
    if not isinstance(listing, Mapping):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the engine returned a malformed listing"}
        )
    for source in listing.get("dynamic_sources") or []:
        if (
            isinstance(source, Mapping)
            and source.get("source_id") == params["ontology_iri"]
        ):
            return listing, source
    return listing, None


async def handle_ontology_import(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Import the approved ontology once; the engine admits it and serves the digest.

    The op passes the schema source and its SHA-256, reads the served digest
    back through the caller's engine client and returns both. A prior import
    of the same ontology with other content refuses ``ONTOLOGY_DRIFT``; an
    ontology already served by another authority refuses
    ``ONTOLOGY_DUPLICATE``. Both refuse before any write. Arbora holds no
    ontology store (ARBORA-DEPLOY-R009.3).
    """
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_models

    try:
        # PIN-LAG: remove after the re-pin that carries the import op.
        require_models(["GraphSchemaOpImportOntology"])
    except PinLagUnavailable as error:
        raise refusal(error) from error
    if _sha256(params["ontology_ttl"]) != params["sha256"]:
        raise OperationRefused(
            "INVALID_ARGUMENT",
            {"reason": "the schema source does not match its sha256"},
        )
    _, existing = await _existing_ontology(context, params)
    if existing is not None:
        origin = existing.get("origin") or {}
        if (
            origin.get("origin") != "pack"
            or origin.get("connector") != params["connector"]
        ):
            raise OperationRefused(
                "ONTOLOGY_DUPLICATE", {"ontology_iri": params["ontology_iri"]}
            )
        served = existing.get("ontology_sha256")
        if isinstance(served, str) and served.removeprefix("sha256:") != params[
            "sha256"
        ].removeprefix("sha256:"):
            raise OperationRefused(
                "ONTOLOGY_DRIFT",
                {"ontology_iri": params["ontology_iri"], "served": served},
            )
    committed = await engine_send(
        context,
        "GraphSchema",
        {
            "op": {
                "op": "import_ontology",
                "connector": params["connector"],
                "content_digest": params["sha256"].removeprefix("sha256:"),
                "expected_prior_revision": params["expected_prior_revision"],
                "ontology_iri": params["ontology_iri"],
                "ontology_ttl": params["ontology_ttl"],
                "shapes_ttl": params["shapes_ttl"],
                "version": params["version"],
            }
        },
        idempotent=True,
    )
    after, _ = await _existing_ontology(context, params)
    served_digest = after.get("composed_digest")
    if not isinstance(committed, Mapping) or not isinstance(served_digest, str):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the engine did not serve the imported digest"}
        )
    return {
        "ontology_iri": params["ontology_iri"],
        "sha256": params["sha256"],
        "served_digest": served_digest,
        "revision": committed.get("revision"),
        "changed": bool(committed.get("changed")),
    }


class ManifestPlanParams(Params):
    name: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)


class ManifestPlanResult(Params):
    manifest_digest: str
    plan_digest: str
    entries: list[dict[str, Any]]
    drift: list[dict[str, Any]]
    destructive: list[dict[str, Any]]
    destructive_digest: str


class ManifestApplyParams(ManifestPlanParams):
    plan_digest: str = Field(pattern=_DIGEST)
    approval_elevation_id: str | None = Field(
        default=None, min_length=1, max_length=256
    )


class ManifestApplyResult(Params):
    manifest_digest: str
    components: list[dict[str, str]]
    receipt_digest: str


def _refused(error: ManifestRefused) -> Any:
    from arbora.api.invoke.pipeline import OperationRefused

    return OperationRefused(error.code, error.details)


def _manifest_root(context: Any) -> Any:
    from pathlib import Path

    from arbora.core.settings import config_dir

    root = context.services.get("deploy_manifest_root")
    return Path(root) if root is not None else config_dir() / "deploy" / "manifests"


def deploy_config_of(context: Any) -> Mapping[str, Any]:
    """The resolved ``deploy.yaml`` (read, never written), or the bound override."""
    config = context.services.get("deploy_config")
    if config is None:
        from arbora.core.settings import config_dir
        from arbora.deployment.image_digests import load_strict

        path = config_dir() / "deploy.yaml"
        config = load_strict(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return config or {}


def _live_tool(context: Any, target: str) -> Mapping[str, Any]:
    """The read tool ``deploy.yaml`` names for ``tools.live_state.<target>``."""
    config = deploy_config_of(context)
    tools = ((config or {}).get("tools") or {}).get("live_state") or {}
    tool = tools.get(target)
    if not (
        isinstance(tool, Mapping)
        and isinstance(tool.get("server"), str)
        and isinstance(tool.get("tool"), str)
    ):
        raise ManifestRefused(
            "LIVE_STATE_UNAVAILABLE", {"missing": f"tools.live_state.{target}"}
        )
    return tool


async def _read_live(context: Any, target: str) -> list[Mapping[str, Any]]:
    tool = _live_tool(context, target)
    invoke = _step_invoke_of(context)
    outcome = await invoke(
        "fleet.call",
        {"server": tool["server"], "tool": tool["tool"], "arguments": {}},
        context.caller,
        f"live-state:{target}",
    )
    code = getattr(outcome, "code", None)
    if code in {"CONFIRMATION_REQUIRED", "STEP_UP_REQUIRED", "PLAN_REQUIRED"}:
        raise ManifestRefused("LIVE_STATE_TOOL_NOT_READ", {"tool": tool["tool"]})
    value = getattr(outcome, "value", None)
    if isinstance(value, Mapping) and isinstance(value.get("value"), Mapping):
        value = value["value"]
    rows = value.get("components") if isinstance(value, Mapping) else None
    if code != "OK" or not isinstance(rows, list):
        raise ManifestRefused("LIVE_STATE_UNAVAILABLE", {"tool": tool["tool"]})
    return [row for row in rows if isinstance(row, Mapping)]


def _step_invoke_of(context: Any) -> Any:
    invoke = context.services.get("plan_step_invoke")
    if invoke is None:
        raise ManifestRefused(
            "UNAVAILABLE", {"reason": "the invoke pipeline is not bound"}
        )
    return invoke


async def _manifest_diff(
    context: Any, params: Mapping[str, Any]
) -> tuple[DeploymentManifest, DeploymentDiff]:
    manifest = load_manifest(_manifest_root(context), params["name"])
    live = await _read_live(context, params["target"])
    return manifest, plan_manifest(manifest, live)


async def handle_manifest_plan(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The typed diff, drift and destructive set of a manifest. Writes nothing."""
    try:
        _, diff = await _manifest_diff(context, params)
    except ManifestRefused as error:
        raise _refused(error) from error
    return {
        "manifest_digest": diff.manifest_digest,
        "plan_digest": diff.plan_digest,
        "entries": list(diff.entries),
        "drift": list(diff.drift),
        "destructive": list(diff.destructive),
        "destructive_digest": diff.destructive_digest,
    }


async def _require_destructive_approval(
    context: Any, diff: DeploymentDiff, elevation_id: str | None
) -> None:
    """A destructive change runs only under a fresh approved elevation of its digest."""
    if not diff.destructive:
        return
    service = context.services.get("elevation_service")
    if service is None:
        raise ManifestRefused(
            "UNAVAILABLE", {"reason": "no elevation service is bound"}
        )
    if elevation_id is None:
        raise ManifestRefused(
            "APPROVAL_REQUIRED", {"destructive": len(diff.destructive)}
        )
    views = {v.elevation_id: v for v in await service.list_elevations()}
    view = views.get(elevation_id)
    if view is None or view.approved_at_ms is None:
        raise ManifestRefused("APPROVAL_REQUIRED", {"elevation_id": elevation_id})
    if view.status != "active" or view.remaining_ms <= 0:
        raise ManifestRefused("APPROVAL_EXPIRED", {"elevation_id": elevation_id})
    if view.justification != f"deploy-destructive:{diff.destructive_digest}":
        raise ManifestRefused("APPROVAL_MISMATCH", {"elevation_id": elevation_id})


async def handle_manifest_apply(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Converge the live state to the manifest; the plan was consumed by the pipeline."""
    require_confirmed_plan(op)
    try:
        manifest, diff = await _manifest_diff(context, params)
        if not diff.entries and not diff.destructive:
            return apply_receipt(manifest, diff)
        if diff.plan_digest != params["plan_digest"]:
            raise ManifestRefused("PLAN_MISMATCH", {"reason": "the diff changed"})
        absent = missing_inputs(manifest, diff.entries)
        if absent:
            raise ManifestRefused("MANIFEST_INPUT_MISSING", {"missing": absent})
        await _require_destructive_approval(
            context, diff, params.get("approval_elevation_id")
        )
        invoke = _step_invoke_of(context)
        tenant, target = str(context.caller.tenant), params["target"]

        async def rollout(component_id: str, key_prefix: str) -> Mapping[str, Any]:
            assembly = await _assemble(context, tenant, target, [component_id])
            graph = assembly.get("graph")
            return await run_confirmed_steps(
                invoke,
                context.caller,
                graph,
                plan_digest=graph_digest(graph),
                key_prefix=key_prefix,
            )

        clock = context.services.get("now_ms")

        async def record(component_id: str, image: str) -> bool:
            return await record_rollout(
                invoke,
                context.caller,
                manifest_digest=diff.manifest_digest,
                component_id=component_id,
                artifact_digest=image,
                target=target,
                now_ms=int(clock()) if callable(clock) else 0,
            )

        receipt = await apply_manifest(manifest, diff, rollout=rollout, record=record)
    except ManifestRefused as error:
        raise _refused(error) from error
    return receipt.as_dict()


def apply_receipt(manifest: DeploymentManifest, diff: DeploymentDiff) -> dict[str, Any]:
    """The receipt of a converged state: the same value the first apply returned."""
    from arbora.deployment.manifest import ApplyReceipt

    return ApplyReceipt(
        diff.manifest_digest,
        tuple(
            {"id": c.id, "revision": c.revision, "outcome": "converged"}
            for c in manifest.components
        ),
    ).as_dict()


#: A replay of a confirmed apply returns the recorded node outcomes: every node
#: is one ``fleet.call`` reserved in the engine effect journal under
#: ``deploy:<plan_digest>:<node_id>`` (ARBORA-DEPLOY-R017.21).
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "deploy.placement.apply": "arbora.mcp_server.effect_journal:EgEffectJournal",
    "deploy.manifest.apply": "arbora.mcp_server.effect_journal:EgEffectJournal",
    # The engine replays ``GraphSchema`` by operation identity (the idempotency key).
    "deploy.ontology.import": "medulla:GraphSchema",
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
        build_write_op(
            op_id="deploy.ontology.import",
            summary="Import the approved ontology once and read back its served digest",
            examples=("import the deployment ontology",),
            params=OntologyImportParams,
            result=OntologyImportResult,
            handler="arbora.api.ops.deploy.handle_ontology_import",
            scope="fleet:control",
        ),
        build_read_op(
            op_id="deploy.manifest.plan",
            summary="Diff a deployment manifest against the live state",
            examples=("what would this deployment manifest change",),
            params=ManifestPlanParams,
            result=ManifestPlanResult,
            handler="arbora.api.ops.deploy.handle_manifest_plan",
            scope="ops:read",
        ),
        build_write_op(
            op_id="deploy.manifest.apply",
            summary="Converge the live state to a deployment manifest",
            examples=("apply the deployment manifest",),
            params=ManifestApplyParams,
            result=ManifestApplyResult,
            handler="arbora.api.ops.deploy.handle_manifest_apply",
            scope="fleet:control",
            effect=Effect.DESTRUCTIVE,
            confirm=Confirm.PLAN,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "OntologyImportParams",
    "OntologyImportResult",
    "PlacementApplyParams",
    "PlacementApplyResult",
    "PlacementPlanParams",
    "PlacementPlanResult",
    "handle_manifest_apply",
    "handle_manifest_plan",
    "handle_ontology_import",
    "handle_placement_apply",
    "handle_placement_plan",
    "operations",
    "specs",
]
