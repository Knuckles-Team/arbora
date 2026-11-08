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


def get_registry() -> Registry:
    """Compose supported declarations and the engine's complete method catalog.

    This requires no live session or service bindings, so canonical generation
    can use the same registry as serving. Bootstrap must separately validate
    live authorities before exposing these declarations as usable operations.
    Provider failures propagate; there is no partial-registry fallback.
    """
    from . import (
        access,
        agents,
        browser,
        capacity,
        decide,
        federation,
        fleet,
        ingest,
        memory,
        messaging,
        models,
        ops,
        plans,
        policy,
        prompts,
        query,
        query_engine,
        research,
        retrieval,
        runs,
        security,
        source,
        swarm,
        telemetry,
        usage,
        work,
    )

    curated: list[OpSpec] = []
    for module in (
        access,
        agents,
        browser,
        capacity,
        decide,
        federation,
        fleet,
        ingest,
        memory,
        messaging,
        models,
        ops,
        plans,
        policy,
        prompts,
        query,
        query_engine,
        research,
        retrieval,
        runs,
        security,
        source,
        swarm,
        telemetry,
        usage,
        work,
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
    catalog = load_catalog()
    scopes = load_generated_scopes(catalog)
    for op in curated:
        missing = (op.scopes | op.executor_scopes) - scopes.keys()
        if missing:
            raise EgContractError(
                f"{op.id}: unregistered curated EG scopes: {', '.join(sorted(missing))}"
            )
    generated = load_eg_bindings(catalog=catalog, curated_ops=curated)
    return Registry((*curated, *generated), catalog=catalog)
