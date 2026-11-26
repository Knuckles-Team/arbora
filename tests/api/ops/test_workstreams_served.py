"""Two harnesses share one workstream over the served MCP surface (ARBORA-HARNESS-PLUGINS-R004.7)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from tests.api._served_support import (
    McpAsk,
    confirmable_invocation,
    person,
    route_methods,
)
from tests.api.test_workstreams import Graph
from tests.test_served_operation_api import _call, served  # noqa: F401

SCOPES = ("workstream:read", "workstream:write")
ROOT = "/w/shared-repo"
ATTACH_P95_BUDGET_S = 0.150


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, int(len(ordered) * 0.95) - 1)]


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Any:
    return confirmable_invocation(monkeypatch)


def test_two_harnesses_share_one_workstream(
    captured: Any,
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, client = served
    graph = Graph()

    def engine(method: str) -> Any:
        async def send(params: dict[str, Any], _principal: str) -> Any:
            return await graph.send(None, method, params)

        return lambda params, principal: _run(send(params, principal))

    methods = (
        "GetNodePropertiesBatch",
        "AddNode",
        "AddEdge",
        "CompareAndSetNodeFields",
        "GetPredecessors",
        "GetNodesByLabel",
    )
    route_methods(
        client,
        {method: engine(method) for method in methods},
        monkeypatch,
        {
            "fixture-verified": person("one", extra_scopes=SCOPES),
            "stranger": person("two", extra_scopes=SCOPES),
        },
    )

    def ask_as(name: str, tenant: str) -> McpAsk:
        return McpAsk(
            captured["invocation"],
            monkeypatch,
            person(name, tenant=tenant, extra_scopes=SCOPES),
        )

    one = ask_as("one", "tenant:one")
    first = one(
        "write",
        "workstream.attach",
        {
            "harness": "claude-code",
            "harness_session_id": "cc-1",
            "workspace_root": ROOT,
        },
        idempotency_key="k-attach-cc",
    )
    assert first["ok"] is True, json.dumps(first)[:1500]
    assert first["result"]["value"]["status"] == "attached"
    checkpoint = one(
        "write",
        "workstream.checkpoint",
        {
            "harness_session_id": "cc-1",
            "workspace_root": ROOT,
            "summary": "left off at step 3",
            "trigger": "stop",
        },
        idempotency_key="k-checkpoint",
    )
    assert checkpoint["ok"] is True, checkpoint

    second = one(
        "write",
        "workstream.attach",
        {"harness": "opencode", "harness_session_id": "oc-1", "workspace_root": ROOT},
        idempotency_key="k-attach-oc",
    )
    context = second["result"]["value"]["context"]
    assert "left off at step 3" in repr(context)
    assert len(graph.edges) == 2

    # Another principal, with the same root, finds nothing: a foreign workstream
    # answers exactly like an absent one. The served fixture binds one tenant, so
    # the second-tenant leg is not proven here (the key embeds the tenant and is
    # covered in tests/api/test_workstreams.py).
    absent = _call(
        http,
        "workstream.get",
        {"workspace_root": ROOT},
        Authorization="Bearer stranger",
    )
    assert absent.json()["error"]["code"] == "WORKSTREAM_NOT_FOUND", absent.text
    assert (
        _call(http, "workstream.get", {"workspace_root": "/w/never-attached"}).json()[
            "error"
        ]["code"]
        == "WORKSTREAM_NOT_FOUND"
    )
    mine = one("ask", "workstream.get", {"workspace_root": ROOT})
    assert mine["ok"] is True and "left off at step 3" in repr(mine)

    one = ask_as("one", "tenant:one")
    samples: list[float] = []
    for index in range(200):
        started = time.perf_counter()
        answer = one(
            "write",
            "workstream.attach",
            {
                "harness": "opencode",
                "harness_session_id": f"load-{index}",
                "workspace_root": ROOT,
            },
            idempotency_key=f"k-load-{index}",
        )
        samples.append(time.perf_counter() - started)
        assert answer["ok"] is True, answer
    assert _p95(samples) <= ATTACH_P95_BUDGET_S


def _run(awaitable: Any) -> Any:
    """Drive a coroutine that never suspends (the in-memory graph) to its result."""
    try:
        awaitable.send(None)
    except StopIteration as done:
        return done.value
    raise AssertionError("the in-memory graph suspended")
