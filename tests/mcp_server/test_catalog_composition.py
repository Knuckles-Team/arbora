"""Startup composition for generated EG and public AU catalog ports."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest

from arbora.fleet.catalog_reader import DeferredFleetCatalogReader
from arbora.mcp_server import catalog_composition as composition


class GraphCompute:
    def __init__(self) -> None:
        self.graphs: list[str] = []

    def for_graph(self, graph: str) -> Any:
        self.graphs.append(graph)
        return SimpleNamespace(async_client=SimpleNamespace(graph=graph))


class Session:
    graph = "tenant__tenant_a____commons__"

    def engine_verified_context(self) -> dict[str, str]:
        return {
            "tenant": "tenant-a",
            "principal": "principal-a",
            "agent_id": "agent-a",
            "audience": "epistemic-graph",
            "policy_version": "policy-a",
        }


class Multiplexer:
    def __init__(self, *, fail: bool = False) -> None:
        self.refreshes = 0
        self.fail = fail

    async def refresh_engine_catalog(self) -> None:
        self.refreshes += 1
        if self.fail:
            raise RuntimeError("catalog unavailable")


@pytest.mark.asyncio
async def test_startup_injects_generated_clients_and_public_catalog_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compute = GraphCompute()
    engine = SimpleNamespace(graph_compute=compute)
    deferred = DeferredFleetCatalogReader()
    mux = Multiplexer()
    workflows = object()
    agents = object()
    installed: list[Any] = []

    monkeypatch.setattr(
        composition,
        "configure_enhanced_catalog",
        lambda authority, *, authority_scope: installed.append(
            (authority, authority_scope)
        ),
    )
    verified: list[dict[str, Any]] = []

    async def verify(**kwargs: Any) -> None:
        verified.append(kwargs)

    monkeypatch.setattr(composition, "verify_semantic_content", verify)
    monkeypatch.setattr(
        composition,
        "required_content_connectors",
        lambda: ("arbora", "agent-utilities"),
    )

    bound: list[str] = []

    @contextlib.contextmanager
    def bind_graph(graph: str) -> Iterator[None]:
        bound.append(graph)
        yield

    reader = await composition.compose_catalog_authorities(
        engine=engine,
        session=Session(),
        deferred_fleet=deferred,
        multiplexer=mux,
        catalog_ports_factory=lambda received_engine, received_session: cast(
            Any,
            (workflows, agents),
        ),
        bind_graph=bind_graph,
    )

    # The tenant's data graph is the session's graph, never the bare tenant id.
    assert compute.graphs == [Session.graph, "__commons__"]
    assert bound == [Session.graph]
    assert mux.refreshes == 1
    assert verified == [
        {
            "client": SimpleNamespace(graph=Session.graph),
            "tenant_id": "tenant-a",
            "graph": Session.graph,
            "connectors": ("arbora", "agent-utilities"),
        }
    ]
    authority, authority_scope = installed[0]
    assert authority._fleet is reader
    assert authority._workflows is workflows
    assert authority._agents is agents
    process_sessions: list[Any] = []
    monkeypatch.setattr(composition, "_use_process_session", process_sessions.append)
    authority_scope()
    assert len(process_sessions) == 1
    assert isinstance(process_sessions[0], Session)
    assert deferred._reader is reader


@pytest.mark.asyncio
async def test_failed_initial_refresh_aborts_startup_without_static_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SimpleNamespace(graph_compute=GraphCompute())
    deferred = DeferredFleetCatalogReader()
    monkeypatch.setattr(
        composition, "configure_enhanced_catalog", lambda value, **_: None
    )

    async def verified(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(composition, "verify_semantic_content", verified)
    monkeypatch.setattr(composition, "required_content_connectors", lambda: ("arbora",))

    with pytest.raises(RuntimeError, match="catalog unavailable"):
        await composition.compose_catalog_authorities(
            engine=engine,
            session=Session(),
            deferred_fleet=deferred,
            multiplexer=Multiplexer(fail=True),
            catalog_ports_factory=lambda engine, session: cast(
                Any, (object(), object())
            ),
            bind_graph=lambda graph: contextlib.nullcontext(),
        )

    # The EG reader is installed, but startup propagates the failed proof and
    # never reaches the serving loop or a static MCP_CONFIG declaration.
    assert deferred._reader is not None


class RetargetableSession(Session):
    def __init__(self, graph: str = Session.graph) -> None:
        self.graph = graph

    def with_graph(self, graph: str) -> RetargetableSession:
        return RetargetableSession(graph)


@pytest.mark.spec("ARBORA-FLEET-R026.2")
@pytest.mark.asyncio
async def test_fleet_catalog_reads_bind_the_process_session_not_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every fleet catalog engine read runs as the process, re-targeted per graph.

    A caller's own session cannot see the process-owned registry rows, so the
    default binder must never resolve the ambient (caller) session.
    """
    from arbora.api.contracts import session as session_module

    process = RetargetableSession()
    bound: list[tuple[Any, str]] = []

    @contextlib.contextmanager
    def use_session(value: Any) -> Iterator[Any]:
        bound.append((value, value.graph))
        yield value

    def ambient_is_forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the fleet catalog must not read the caller session")

    monkeypatch.setattr(session_module, "use_session", use_session)
    monkeypatch.setattr(session_module, "resolve_session", ambient_is_forbidden)
    monkeypatch.setattr(
        composition, "configure_enhanced_catalog", lambda value, **_: None
    )

    async def verified(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(composition, "verify_semantic_content", verified)
    monkeypatch.setattr(composition, "required_content_connectors", lambda: ())
    deferred = DeferredFleetCatalogReader()
    await composition.compose_catalog_authorities(
        engine=SimpleNamespace(graph_compute=GraphCompute()),
        session=process,
        deferred_fleet=deferred,
        multiplexer=Multiplexer(),
        catalog_ports_factory=lambda engine, session: cast(Any, (object(), object())),
    )
    port = deferred._reader._port  # type: ignore[union-attr]
    with port._bind_graph("__commons__"):
        pass

    assert [graph for _, graph in bound] == [Session.graph, "__commons__"]
    assert all(isinstance(value, RetargetableSession) for value, _ in bound)
