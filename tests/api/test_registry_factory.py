"""Offline assembly retains exact provider coverage and rejects broken declarations."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from arbora.api.ops import (
    access,
    agents,
    browser,
    capacity,
    decide,
    federation,
    fleet,
    get_registry,
    ingest,
    memory,
    messaging,
    models,
    ops,
    plans,
    policy,
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
from arbora.api.registry import (
    Composite,
    EgMethod,
    EgSchemaRef,
    Executor,
    SubjectRef,
    SubjectSource,
    Verb,
)
from arbora.api.registry.eg_binding import EgContractError
from tests.api._catalog import method_row, scope_row
from tests.api.test_eg_binding import _curated, _fixture

_CURATED_SCOPES = (
    "query:unified",
    "query:nl",
    "kg:read",
    "kg:write",
    "mcp:discover",
    "mcp:delegate",
    "rbac:elevation",
    "rbac:elevation-read",
    "rbac:approve-elevation",
    "lease:read",
    "security:check",
    "security:read",
    "explain:read",
    "capacity:read",
    "capacity:admin",
    "ingest:write",
    "federation:read",
    "sparql:read",
    "query:sql",
    "validation:read",
    "owl:read",
    "federation:admin",
    "kv:read",
    "kv:write",
    "ingest:read",
    "decide:commit",
    "telemetry:read",
    "policy:read",
    "usage:read",
    "swarm:read",
    "memory:read",
    "memory:write",
    "work:read",
    "work:write",
    "ops:read",
    "ops:admin",
    "node:read",
    "edge:read",
    "blob:read",
    "agent:pack-control",
    "fleet:control",
    "mcp:admin",
    "registry:read",
)

_ENGINE_METHODS = {
    "Uql",
    "NlQuery",
    "Sparql",
    "Sql",
    "OntologyInspect",
    "GraphSchemaClasses",
    "ShaclValidate",
    "QueryForeignColumns",
    "RegisterForeignSource",
}


@pytest.fixture
def provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, Path]:
    """Serve an in-memory method catalog in place of the installed engine's."""
    from arbora.api.registry import eg_binding

    document, exclusions = _fixture(tmp_path)
    # Synthetic approved scope evidence; production classifications are provider-owned.
    document["scopes"].extend(scope_row(scope, "user") for scope in _CURATED_SCOPES)
    # The engine methods curated operations bind, with the curated scope.
    for module in (query, query_engine):
        for op in module.operations():
            if isinstance(op.binding, EgMethod):
                document["methods"].append(
                    method_row(
                        op.binding.service,
                        next(iter(op.scopes)),
                        mutates=op.verb is not Verb.ASK,
                    )
                )
    monkeypatch.setattr(eg_binding, "_installed_catalog", lambda: document)
    monkeypatch.setattr(eg_binding, "_EXCLUSIONS", exclusions)
    return document, exclusions


def test_no_argument_factory_is_deterministic_and_complete(
    provider: tuple[dict, Path],
) -> None:
    assert not inspect.signature(get_registry).parameters
    first, second = get_registry(), get_registry()
    assert first is not second
    assert first.canonical == second.canonical
    assert first.digest == second.digest
    declared = (
        *access.operations(),
        *agents.operations(),
        *browser.operations(),
        *capacity.operations(),
        *decide.operations(),
        *federation.operations(),
        *fleet.operations(),
        *ingest.operations(),
        *memory.operations(),
        *messaging.operations(),
        *models.operations(),
        *security.operations(),
        *telemetry.operations(),
        *usage.operations(),
        *policy.operations(),
        *query.operations(),
        *query_engine.operations(),
        *research.operations(),
        *retrieval.operations(),
        *runs.operations(),
        *source.operations(),
        *swarm.operations(),
        *work.operations(),
        *ops.operations(),
        *plans.operations(),
    )
    assert {op.id for op in first} == {op.id for op in declared} | {
        "eg.query.Read",
        "eg.query.Admin",
        "eg.query.Service",
    }
    assert all(first[op.id] == op for op in declared)


def test_only_exact_curated_method_is_suppressed(
    provider: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agents, "operations", lambda: (_curated("Read", "query.read"),))
    registry = get_registry()
    bindings = [
        op.binding.service for op in registry if isinstance(op.binding, EgMethod)
    ]
    assert sorted(bindings) == sorted(
        ["Admin", "Read", "Service", *sorted(_ENGINE_METHODS)]
    )
    assert registry.get("eg.query.Read") is None
    curated = registry["query.read"].binding
    assert isinstance(curated, EgMethod)
    assert curated.service == "Read"
    assert registry.get("eg.query.Admin") is not None
    assert registry.get("eg.query.Service") is not None


def test_duplicate_curated_ids_are_rejected(
    provider: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = agents.operations()
    monkeypatch.setattr(agents, "operations", lambda: (*operations, operations[0]))
    with pytest.raises(ValueError, match="duplicate operation id"):
        get_registry()


@pytest.mark.parametrize(
    "handler",
    [
        "arbora.api.ops.agents.absent",
        "arbora.api.ops.agents.operations",
        "unapproved.module.handler",
        "arbora.api.ops.browser.handle_browser",
    ],
)
def test_unsupported_or_unbound_handler_is_rejected(
    provider: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch, handler: str
) -> None:
    op = agents.operations()[0].model_copy(
        update={"binding": Composite(handler=handler)}
    )
    monkeypatch.setattr(agents, "operations", lambda: (op,))
    with pytest.raises(ValueError, match="unsupported or unbound handler"):
        get_registry()


def test_missing_provider_contract_never_returns_partial_registry(
    provider: tuple[dict, Path],
) -> None:
    document, _ = provider
    document["methods"] = []
    with pytest.raises(EgContractError, match="requires nonempty methods"):
        get_registry()


def test_unapproved_provider_scope_is_not_inferred(provider: tuple[dict, Path]) -> None:
    document, _ = provider
    document["methods"][0]["policy"]["authz_action"] = "pending:classification"
    with pytest.raises(EgContractError, match="unregistered EG scope"):
        get_registry()


def test_reviewed_exclusion_is_used_and_rot_rejected(
    provider: tuple[dict, Path],
) -> None:
    _, exclusions = provider
    exclusions.write_text(
        "exclusions:\n  - method: Read\n    reason: engine-internal\n"
    )
    registry = get_registry()
    assert registry.get("eg.query.Read") is None
    assert registry.get("eg.query.Admin") is not None
    exclusions.write_text(
        "exclusions:\n  - method: Gone\n    reason: engine-internal\n"
    )
    with pytest.raises(EgContractError, match="stale EG exclusion"):
        get_registry()


def test_stale_curated_binding_is_rejected(
    provider: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agents, "operations", lambda: (_curated("Gone", "query.read"),))
    with pytest.raises(EgContractError, match="stale EG binding"):
        get_registry()


def test_provider_order_does_not_change_digest(provider: tuple[dict, Path]) -> None:
    document, _ = provider
    before = get_registry()
    document["methods"].reverse()
    assert get_registry().canonical == before.canonical


def test_catalog_digest_change_moves_the_registry_digest(
    provider: tuple[dict, Path],
) -> None:
    document, _ = provider
    before = get_registry()
    document["catalog_digest"] = "d" * 64
    assert get_registry().digest != before.digest


def test_duplicate_provider_ownership_is_rejected(
    provider: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        agents,
        "operations",
        lambda: (_curated("Read", "query.first"), _curated("Read", "query.second")),
    )
    with pytest.raises(EgContractError, match="duplicate EG binding"):
        get_registry()


def test_curated_and_generated_id_collision_is_rejected(
    provider: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    op = agents.operations()[0].model_copy(update={"id": "eg.query.Read"})
    monkeypatch.setattr(agents, "operations", lambda: (op,))
    with pytest.raises(ValueError, match="duplicate operation id"):
        get_registry()


@pytest.mark.parametrize(
    "scope", ["kg:read", "kg:write", "mcp:discover", "mcp:delegate"]
)
def test_missing_composite_caller_scope_fails_before_canonicalization(
    provider: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    from arbora.api.ops import registry_factory

    document, _ = provider
    document["scopes"] = [row for row in document["scopes"] if row["scope"] != scope]

    def unexpected_registry(*args: object, **kwargs: object) -> None:
        pytest.fail("unregistered scope reached canonical registry construction")

    monkeypatch.setattr(registry_factory, "Registry", unexpected_registry)
    with pytest.raises(
        EgContractError, match=f"unregistered curated EG scopes: {scope}"
    ):
        get_registry()


def test_composite_executor_scope_requires_provider_evidence(
    provider: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    op = agents.operations()[0].model_copy(
        update={
            "executor": Executor.SERVICE,
            "executor_scopes": frozenset({"fixture:executor"}),
            "subject": SubjectRef(source=SubjectSource.CALLER_TENANT),
        }
    )
    monkeypatch.setattr(agents, "operations", lambda: (op,))
    with pytest.raises(
        EgContractError, match="unregistered curated EG scopes: fixture:executor"
    ):
        get_registry()
    document, _ = provider
    document["scopes"].append(scope_row("fixture:executor", "service-only"))
    assert get_registry()[op.id] == op


def test_curated_scope_evidence_uses_provider_class_validation(
    provider: tuple[dict, Path],
) -> None:
    document, _ = provider
    for row in document["scopes"]:
        if row["scope"] == "mcp:delegate":
            row["class"] = "unapproved"
    with pytest.raises(EgContractError, match="invalid EG scope class"):
        get_registry()


@pytest.mark.spec("ARBORA-OPS-R024.4", "ARBORA-OPS-R847")
def test_memory_and_kv_operations_are_registered() -> None:
    from arbora.api.ops.registry_factory import get_registry

    ids = {op.id for op in get_registry()}
    assert {"memory.read", "memory.write", "memory.list", "memory.delete"} <= ids
    assert {f"eg.storage.Kv{m}" for m in ("Get", "Put", "Scan", "Delete", "Cas")} <= ids


@pytest.mark.spec("ARBORA-OPS-R068.5")
def test_harness_operations_unregistered_until_engine_senders() -> None:
    """ARBORA-OPS-R068.5: no ``harness.*`` op is served before EG ships its senders."""
    import importlib
    import importlib.util

    from arbora.api.ops.registry_factory import get_registry

    module = importlib.util.find_spec("medulla.generated.orchestration")
    senders = (
        "send_list_harness_components",
        "send_current_harness_component",
        "send_retrieve_lessons",
        "send_harness_evolution_report",
    )
    published = module is not None and all(
        hasattr(importlib.import_module("medulla.generated.orchestration"), s)
        for s in senders
    )
    served = {op.id for op in get_registry() if op.id.startswith("harness.")}
    assert published or not served


@pytest.mark.spec("ARBORA-FLEET-R012.1")
def test_engine_operations_come_from_served_descriptors(
    provider: tuple[dict, Path],
) -> None:
    """The registry is built from the run-time catalog; no contract file is read."""
    document, _ = provider
    registry = get_registry()
    params = registry["eg.query.Read"].params
    assert isinstance(params, EgSchemaRef)
    assert params.path == "medulla:method/Read#request"
    before = registry.digest
    document["catalog_digest"] = "f" * 64
    assert get_registry().digest != before
    document.pop("catalog_digest")
    with pytest.raises(EgContractError, match="catalog_digest"):
        get_registry()
