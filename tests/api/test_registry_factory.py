"""Offline assembly retains exact provider coverage and rejects broken declarations."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from arbora.api.ops import (
    agents,
    get_registry,
    harness,
    identity,
    query,
    query_engine,
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
    "workstream:read",
    "workstream:write",
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
    "capacity:lease",
    "ingest:write",
    "federation:read",
    "finance:read",
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
    "blob:write",
    "graph:write",
    "agent:pack-control",
    "fleet:control",
    "mcp:admin",
    "registry:read",
    "identity:self",
    "identity:admin",
    "approvals:read",
    "approvals:decide",
    "query:write",
    "profile:write",
    "chats:own",
    "fleet:read",
    "agent:component-write",
    "workspace:read",
    "workspace:write",
    "telemetry:write",
    "agent:library-read",
    "agent:library-write",
    "loops:read",
    "loops:control",
    "finance:propose-order",
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


def _engine_scope_rows(listed_rows: list[dict]) -> list[dict]:
    """The installed engine catalog's scopes the fixture does not already list.

    The engine catalog is the scope authority: serving every scope it lists means a curated
    operation naming a real engine scope never trips on a stale hand-kept list. The list
    above only adds the Arbora-owned scopes the engine lacks.
    """
    from arbora.api.registry import eg_binding

    listed = {row["scope"] for row in listed_rows}
    return [
        row for row in eg_binding.load_catalog()["scopes"] if row["scope"] not in listed
    ]


@pytest.fixture
def provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, Path]:
    """Serve an in-memory method catalog in place of the installed engine's."""
    from arbora.api.registry import eg_binding

    document, exclusions = _fixture(tmp_path)
    # Synthetic approved scope evidence; production classifications are provider-owned.
    document["scopes"].extend(scope_row(scope, "user") for scope in _CURATED_SCOPES)
    document["scopes"].extend(_engine_scope_rows(document["scopes"]))
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


#: Modules the factory leaves uncomposed on purpose: every call to their operations could
#: only answer ``UNAVAILABLE`` until the engine serves the city state and the run trace,
#: so they are not listed (pinned by ``test_city_state_operations_unregistered_until_served``).
_UNCOMPOSED = frozenset({"presence", "activity"})


def _declared_operations() -> list:
    """Every operation any ``arbora.api.ops`` module declares, found by walking the package.

    A hand-kept module list went stale as modules were added; walking the package makes the
    test fail when the factory composes fewer modules than the package declares.
    """
    import importlib
    import pkgutil

    import arbora.api.ops as package

    declared: list = []
    for info in pkgutil.iter_modules(package.__path__):
        if info.name in _UNCOMPOSED:
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        operations = getattr(module, "operations", None)
        if callable(operations):
            declared.extend(operations())
    return declared


def test_no_argument_factory_is_deterministic_and_complete(
    provider: tuple[dict, Path],
) -> None:
    assert not inspect.signature(get_registry).parameters
    first, second = get_registry(), get_registry()
    assert first is not second
    assert first.canonical == second.canonical
    assert first.digest == second.digest
    declared = _declared_operations()
    assert {op.id for op in first} == {op.id for op in declared} | {
        "eg.query.Read",
        "eg.query.Admin",
        "eg.query.Service",
    }
    from arbora.api.registry.pending_scopes import apply_pending_scopes

    served, errors = apply_pending_scopes(
        declared, {row["scope"] for row in provider[0]["scopes"]}
    )
    assert errors == []
    assert all(first[op.id] == op for op in served)


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


@pytest.mark.spec("ARBORA-IDENTITY-R020")
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
def test_harness_operations_registered_once_in_the_registry() -> None:
    """ARBORA-OPS-R068.5: the registry is the authority; a served ``harness.*`` op is registered."""
    from arbora.api.ops.registry_factory import get_registry

    served = {op.id for op in get_registry() if op.id.startswith("harness.")}
    assert served == {op.id for op in harness.operations()}


@pytest.mark.spec(
    "ARBORA-OPS-R915", "ARBORA-OPS-R915.1", "ARBORA-OPS-R916", "ARBORA-FLEET-R084"
)
def test_city_state_operations_unregistered_until_served() -> None:
    """The presence and activity operations are not served before the engine publishes
    the city-state and run-trace wire surface; the layout, chunk, edit-layer and rules
    operations are served now, from plain graph reads (ARBORA-FLEET-R084 to R088).
    """
    import importlib.util

    from arbora.api.ops.registry_factory import get_registry
    from arbora.api.registry.eg_binding import load_catalog

    served = {op.id for op in get_registry() if op.id.startswith("fleet.city.")}
    assert {
        "fleet.city.layout",
        "fleet.city.chunk",
        "fleet.city.rules",
        "fleet.city.edit_layer.get",
        "fleet.city.edit_layer.put",
    } <= served
    world_view = importlib.util.find_spec("medulla.generated.world_view") is not None
    run_trace = any(m["id"] == "RunMetrics" for m in load_catalog()["methods"])
    assert (world_view or run_trace) or not (
        served & {"fleet.city.presence", "fleet.city.activity"}
    )


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


@pytest.mark.spec("ARBORA-IDENTITY-R025")
def test_identity_operations_are_registered() -> None:
    """Identity administration is composed unconditionally and needs ``identity:admin``."""
    import inspect

    from arbora.api.ops import registry_factory
    from arbora.security.scope_registry import session_scopes

    composed = inspect.getsource(registry_factory.get_registry)
    assert "        identity,\n" in composed
    operations = {op.id: op for op in identity.operations()}
    assert {
        "identity.self.get",
        "identity.users.list",
        "identity.roles.assign",
        "identity.providers.upsert",
        "identity.mode.transition_step",
        "identity.audit.verify",
    } <= set(operations)
    assert "identity:admin" in session_scopes()
    assert "identity:admin" in operations["identity.users.list"].scopes
