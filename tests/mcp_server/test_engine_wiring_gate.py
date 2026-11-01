"""Gate G-26 (arbora): the MCP surface arbora serves is wired to epistemic-graph.

Formerly an agent-utilities certification gate; arbora serves the MCP intent
and engine surfaces, so it proves here that what it serves resolves to the
methods the installed epistemic-graph client actually exposes.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

pytest.importorskip("medulla")

from medulla import client as eg_client  # noqa: E402

from arbora.mcp_server.backing.tools import engine_tools, query_tools  # noqa: E402


@pytest.mark.spec("ARBORA-HOST-R001.8")
def test_every_engine_domain_family_resolves_to_an_eg_client_class():
    missing = {
        domain: name
        for domain, name in engine_tools._DOMAIN_CLASSES.items()
        if getattr(eg_client, name, None) is None
    }
    assert engine_tools.ENGINE_DOMAINS, "no engine_<domain> families discovered"
    assert set(engine_tools.ENGINE_DOMAINS) <= set(engine_tools._DOMAIN_CLASSES)
    assert not set(missing) & set(engine_tools.ENGINE_DOMAINS)


@pytest.mark.spec("ARBORA-HOST-R001.8")
def test_every_served_engine_method_is_an_eg_coroutine():
    for domain, methods in engine_tools.ENGINE_DOMAINS.items():
        cls = getattr(eg_client, engine_tools._DOMAIN_CLASSES[domain])
        for method in methods:
            assert inspect.iscoroutinefunction(getattr(cls, method)), (
                f"engine_{domain}.{method} is not served by epistemic-graph"
            )


@pytest.mark.spec("ARBORA-HOST-R001.8")
def test_query_uql_dispatches_to_the_engine_uql_method():
    calls: list[tuple[str, dict[str, Any]]] = []

    class _Engine:
        def uql(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            calls.append((query, kwargs))
            return [{"n": 1}]

    assert any(
        callable(getattr(getattr(eg_client, name), "uql", None))
        for name in dir(eg_client)
        if name.endswith("Client") and inspect.isclass(getattr(eg_client, name))
    ), "installed epistemic-graph exposes no Uql method"
    rows = query_tools._run_graph_query_uql_rows(
        "MATCH (n) RETURN n LIMIT 1", _Engine()
    )
    assert calls and calls[0][0] == "MATCH (n) RETURN n LIMIT 1"
    assert rows is not None


@pytest.mark.spec("ARBORA-HOST-R001.8")
def test_installed_eg_query_client_owns_the_nl_planner():
    planner = getattr(eg_client.QueryClient, "nl_query", None)
    assert inspect.iscoroutinefunction(planner), "EG exposes no nl_query planner"
    assert "nl_query" in engine_tools.ENGINE_DOMAINS["query"]
    assert engine_tools.action_policy("query", "nl_query")["scope"] == "kg:read"


@pytest.mark.spec("ARBORA-HOST-R001.8")
@pytest.mark.parametrize("tool", ["nl_query", "graph_ask", "ask_data"])
def test_natural_language_tools_plan_through_eg_nl_query(monkeypatch, tool):
    """Every served NL tool reaches EG ``QueryClient.nl_query`` and nothing else."""
    import json

    from arbora.mcp_server import runtime

    calls: list[tuple[str, str, dict[str, Any]]] = []

    def _dispatch(domain, methods, action, params_json, graph):
        calls.append((domain, action, json.loads(params_json)))
        assert "nl_query" in methods
        return json.dumps([{"id": "n1", "name": "alpha"}, {"id": "n2"}])

    monkeypatch.setattr(engine_tools, "_dispatch", _dispatch)
    monkeypatch.setattr(
        runtime,
        "_get_engine",
        lambda: pytest.fail("NL planning must not use a local engine planner"),
    )
    runtime.ensure_tools_registered()
    func = runtime.REGISTERED_TOOLS[tool]
    text_arg = "question" if tool in {"graph_ask", "ask_data"} else "text"
    kwargs: dict[str, Any] = {text_arg: "which nodes are alpha?", "limit": 1}
    if tool != "ask_data":
        kwargs["execute"] = True
    if tool == "nl_query":
        kwargs["schema_hint"] = ""
    if tool == "graph_ask":
        kwargs["include_epistemic"] = False
    kwargs["dialect"] = "auto"
    if tool == "ask_data":
        kwargs["max_corrections"] = 2

    result = func(**kwargs)

    assert calls == [("query", "nl_query", {"text": "which nodes are alpha?"})]
    rendered = result if isinstance(result, str) else result.model_dump_json()
    assert "n1" in rendered and "n2" not in rendered


@pytest.mark.spec("ARBORA-HOST-R001.8")
def test_no_local_nl_planner_is_imported_by_the_served_surface():
    from pathlib import Path

    root = Path(engine_tools.__file__).resolve().parents[3]
    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if any(
            needle in path.read_text(encoding="utf-8")
            for needle in (
                "knowledge_graph.core.nl_planner",
                "knowledge_graph.core.nl_query",
                "orchestration import data_analyst",
            )
        )
    ]
    assert offenders == []
