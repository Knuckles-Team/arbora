"""Offline declaration assembly; serving authority validation belongs to bootstrap."""

from __future__ import annotations

from inspect import iscoroutinefunction

from arbora.api.registry import Composite, OpSpec, Registry
from arbora.api.registry.eg_binding import (
    EgContractError,
    load_catalog,
    load_eg_bindings,
    load_generated_scopes,
)
from arbora.api.registry.pending_scopes import apply_pending_scopes


def get_registry() -> Registry:
    """Compose supported declarations and the engine's complete method catalog.

    This requires no live session or service bindings, so canonical generation
    can use the same registry as serving. Bootstrap must separately validate
    live authorities before exposing these declarations as usable operations.
    Provider failures propagate; there is no partial-registry fallback.
    """
    from . import (
        a2a_media,
        access,
        agent_library,
        agents,
        approvals,
        assistants,
        atlas,
        browser,
        capacity,
        catalog,
        chats,
        config_layers,
        dashboard,
        decide,
        decision_evidence,
        deploy,
        development,
        documents,
        ecosystem,
        federation,
        finance_receipts,
        fleet,
        goals,
        governance,
        graph_limits,
        identity,
        ingest,
        knowledge,
        memory,
        messaging,
        models,
        ontology_objects,
        operator_surface,
        ops,
        participants,
        plans,
        policy,
        preferences,
        prompts,
        query,
        query_engine,
        research,
        retrieval,
        runs,
        schedules,
        schema,
        security,
        sessions,
        skills_discovery,
        skills_eval,
        skills_friction,
        skills_telemetry,
        skills_tier,
        source,
        swarm,
        telemetry,
        usage,
        work,
        workspace_files,
        workstreams,
    )

    curated: list[OpSpec] = []
    for module in (
        a2a_media,
        access,
        agent_library,
        agents,
        approvals,
        assistants,
        browser,
        capacity,
        catalog,
        chats,
        config_layers,
        decide,
        development,
        dashboard,
        decision_evidence,
        deploy,
        documents,
        ecosystem,
        federation,
        governance,
        graph_limits,
        finance_receipts,
        fleet,
        identity,
        atlas,
        goals,
        ingest,
        ontology_objects,
        knowledge,
        memory,
        messaging,
        models,
        operator_surface,
        ops,
        participants,
        plans,
        preferences,
        policy,
        prompts,
        query,
        query_engine,
        research,
        retrieval,
        runs,
        schedules,
        schema,
        security,
        sessions,
        skills_discovery,
        skills_eval,
        skills_friction,
        skills_telemetry,
        skills_tier,
        source,
        swarm,
        telemetry,
        usage,
        work,
        workspace_files,
        workstreams,
    ):
        for op in module.operations():
            if isinstance(op.binding, Composite):
                namespace, _, name = op.binding.handler.rpartition(".")
                handler = getattr(module, name, None)
                if namespace != module.__name__ or not iscoroutinefunction(handler):
                    raise ValueError(
                        f"{op.id}: unsupported or unbound handler {op.binding.handler}"
                    )
            curated.append(op)
    engine_catalog = load_catalog()
    scopes = load_generated_scopes(engine_catalog)
    curated, errors = apply_pending_scopes(curated, scopes.keys())
    if errors:
        raise EgContractError("; ".join(errors))
    generated = load_eg_bindings(catalog=engine_catalog, curated_ops=curated)
    return Registry((*curated, *generated), catalog=engine_catalog)
