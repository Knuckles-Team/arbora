"""Served-path regressions found by the 0.1.5 live acceptance run."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

# ── RunEvents: created through CreateNodeIfAbsent ────────────────────────────


def test_run_events_are_created_not_upserted(monkeypatch) -> None:
    """0.1.5: every RunEvent AddNode was refused (INTERNAL, unclassified).

    On the live 0.1.5 pod EG 2.27.2 refused ``AddNode`` on the session graph
    while ``CreateNodeIfAbsent`` on the same graph was admitted, so agent runs
    recorded no RunEvent. A RunEvent id is unique: it is a create.
    """
    from arbora import agent_runs

    created: list[tuple[str, str, dict[str, Any]]] = []

    class _Engine:
        def create_node(self, node_id: str, label: str, properties: Any) -> bool:
            created.append((node_id, label, dict(properties)))
            return True

        def add_node(self, *args: Any) -> str:  # pragma: no cover - must not run
            raise AssertionError("RunEvents must not upsert through AddNode")

    monkeypatch.setattr(agent_runs, "_engine", _Engine)
    agent_runs.record_run_event({"type": "tool_call", "run_id": "a2a-1", "step": 2})
    [(node_id, label, properties)] = created
    assert label == "RunEvent" and node_id.startswith("runevent:a2a-1:")
    assert properties["event_type"] == "tool_call" and properties["step"] == 2


def test_the_served_engine_creates_through_create_node_if_absent(monkeypatch) -> None:
    """The served engine's create path sends EG ``CreateNodeIfAbsent``."""
    import msgpack
    from epistemic_graph.generated import graph as generated

    from arbora.mcp_server import served_engine as served

    sent: list[tuple[dict[str, Any], str, str | None]] = []

    async def send(client: Any, params: Any, graph: Any, *, idempotency_key=None):
        sent.append((params, graph, idempotency_key))
        return True

    monkeypatch.setattr(generated, "send_create_node_if_absent", send)
    view = SimpleNamespace(graph_name="tenant__t__g")
    engine = served.ServedEngine.__new__(served.ServedEngine)
    monkeypatch.setattr(engine, "_session_client", lambda: view, raising=False)
    assert engine.create_node("runevent:r:1", "RunEvent", {"run_id": "r"}) is True
    [(params, graph, key)] = sent
    assert graph == "tenant__t__g" and key == "arbora:create-node:runevent:r:1"
    assert params["node_id"] == "runevent:r:1"
    assert msgpack.unpackb(params["properties_msgpack"]) == {
        "run_id": "r",
        "type": "RunEvent",
    }


def test_a_refused_run_event_logs_the_engine_code(monkeypatch, caplog) -> None:
    from arbora import agent_runs

    class _Refusal(Exception):
        code = "INTERNAL"
        detail = "unclassified engine refusal"

    class _Engine:
        def create_node(self, *args: Any) -> bool:
            raise _Refusal()

    monkeypatch.setattr(agent_runs, "_engine", _Engine)
    agent_runs.record_run_event({"type": "error", "run_id": "a2a-1"})
    assert "INTERNAL unclassified engine refusal" in caplog.text


# ── fleet.catalog.search: a server-scoped empty query lists ──────────────────


def _catalog() -> Any:
    from arbora.fleet.catalog_items import CatalogItem, FleetCatalog

    items = (
        CatalogItem(
            id="fleet:tool:searxng-mcp/web_search",
            kind="tool",
            name="web_search",
            description="search",
            server="searxng-mcp",
        ),
        CatalogItem(
            id="fleet:tool:other-mcp/ping",
            kind="tool",
            name="ping",
            description="ping",
            server="other-mcp",
        ),
    )

    async def source() -> Any:
        return items

    async def visible(_item: Any, _caller: Any) -> bool:
        return True

    return FleetCatalog((source,), visible)


_CALLER = SimpleNamespace(effective_scopes=frozenset({"mcp:discover"}))


def test_a_server_scoped_empty_query_lists_that_servers_tools() -> None:
    """0.1.5: ``servers=[...]`` with ``query=""`` raised ValueError (HTTP 500).

    The acceptance suite and the TUI client list one server's tools this way.
    """
    result = asyncio.run(
        _catalog().search(_CALLER, query="", kinds=("tool",), servers=("searxng-mcp",))
    )
    assert [item["name"] for item in result["items"]] == ["web_search"]


def test_an_unscoped_empty_query_still_needs_browse() -> None:
    with pytest.raises(ValueError):
        asyncio.run(_catalog().search(_CALLER, query=""))
    listed = asyncio.run(_catalog().search(_CALLER, query="", browse=True))
    assert len(listed["items"]) == 2


@pytest.mark.spec("ARBORA-A2A-R009.1")
def test_served_cypher_uses_public_row_decoder(monkeypatch) -> None:
    """Native rows are MessagePack blobs, not already named dictionaries."""
    import msgpack
    from epistemic_graph.client import QueryClient

    from arbora.mcp_server import served_engine as served

    calls = []

    class Client:
        graph_name = "tenant__local"

        async def _send(self, method, params, graph=None, **kwargs):
            calls.append((method, params, graph))
            return {
                "columns": ["payload"],
                "rows": [msgpack.packb(['{"reply":"durable"}'], use_bin_type=True)],
            }

    view = SimpleNamespace(query=QueryClient(Client()))
    engine = served.ServedEngine.__new__(served.ServedEngine)
    monkeypatch.setattr(engine, "_session_client", lambda: view, raising=False)
    assert engine.query_cypher(
        "MATCH (e) WHERE e.run_id = $run_id RETURN e.payload AS payload",
        {"run_id": "a2a-proof"},
    ) == [{"payload": '{"reply":"durable"}'}]
    assert len(calls) == 1
    assert calls[0][1]["mode"] == "read"
    assert "a2a-proof" in calls[0][1]["query"]
