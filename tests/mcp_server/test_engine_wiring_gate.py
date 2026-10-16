"""Gate G-26 (graph-os): the MCP surface graph-os serves is wired to epistemic-graph.

Formerly an agent-utilities certification gate; graph-os serves the MCP intent
and engine surfaces, so it proves here that what it serves resolves to the
methods the installed epistemic-graph client actually exposes.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

pytest.importorskip("epistemic_graph")

from epistemic_graph import client as eg_client  # noqa: E402

from graph_os.mcp_server.backing.tools import engine_tools, query_tools  # noqa: E402


@pytest.mark.spec("GRAPHOS-HOST-R001.8")
def test_every_engine_domain_family_resolves_to_an_eg_client_class():
    missing = {
        domain: name
        for domain, name in engine_tools._DOMAIN_CLASSES.items()
        if getattr(eg_client, name, None) is None
    }
    assert engine_tools.ENGINE_DOMAINS, "no engine_<domain> families discovered"
    assert set(engine_tools.ENGINE_DOMAINS) <= set(engine_tools._DOMAIN_CLASSES)
    assert not set(missing) & set(engine_tools.ENGINE_DOMAINS)


@pytest.mark.spec("GRAPHOS-HOST-R001.8")
def test_every_served_engine_method_is_an_eg_coroutine():
    for domain, methods in engine_tools.ENGINE_DOMAINS.items():
        cls = getattr(eg_client, engine_tools._DOMAIN_CLASSES[domain])
        for method in methods:
            assert inspect.iscoroutinefunction(getattr(cls, method)), (
                f"engine_{domain}.{method} is not served by epistemic-graph"
            )


@pytest.mark.spec("GRAPHOS-HOST-R001.8")
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
