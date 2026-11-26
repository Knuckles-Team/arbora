"""Two harnesses share one workstream over the served MCP surface (ARBORA-HARNESS-PLUGINS-R004.7)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from tests.api._served_support import (
    TENANT,
    TENANT_TWO,
    TwoTenants,
    confirmable,  # noqa: F401
    two_tenant_served,  # noqa: F401
)
from tests.api.test_workstreams import Graph
from tests.test_served_operation_api import served  # noqa: F401

ROOT = "/w/shared-repo"
ATTACH_P95_BUDGET_S = 0.150


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, int(len(ordered) * 0.95) - 1)]


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R004.7")
def test_two_harnesses_share_one_workstream(
    two_tenant_served: TwoTenants,  # noqa: F811
) -> None:
    graph = Graph()

    def engine(method: str) -> Any:
        async def send(params: dict[str, Any], _principal: str) -> Any:
            return await graph.send(None, method, params)

        return lambda params, principal: _run(send(params, principal))

    two_tenant_served.route(
        {
            method: engine(method)
            for method in (
                "GetNodePropertiesBatch",
                "AddNode",
                "AddEdge",
                "CompareAndSetNodeFields",
                "GetPredecessors",
                "GetNodesByLabel",
            )
        }
    )
    one = two_tenant_served.mcp(TENANT)
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

    # A principal of the second tenant names the same root and finds nothing: a
    # foreign workstream answers exactly like an absent one.
    absent = two_tenant_served.call(
        TENANT_TWO, "workstream.get", {"workspace_root": ROOT}
    )
    assert absent.json()["error"]["code"] == "WORKSTREAM_NOT_FOUND", absent.text
    unknown = two_tenant_served.call(
        TENANT, "workstream.get", {"workspace_root": "/w/never-attached"}
    )
    assert {**unknown.json()["error"], "request_id": ""} == {
        **absent.json()["error"],
        "request_id": "",
    }
    foreign_mcp = two_tenant_served.mcp(TENANT_TWO)(
        "ask", "workstream.get", {"workspace_root": ROOT}
    )
    assert "left off at step 3" not in repr(foreign_mcp)
    assert (
        two_tenant_served.call(
            TENANT, "workstream.get", {"workspace_root": ROOT}, credential_of=TENANT_TWO
        ).json()["error"]["code"]
        == "UNAUTHENTICATED"
    )
    assert not any(node.get("tenant") == TENANT_TWO for node in graph.nodes.values()), (
        "the second tenant must not have written anything"
    )
    one = two_tenant_served.mcp(TENANT)  # the MCP process session is per surface
    mine = one("ask", "workstream.get", {"workspace_root": ROOT})
    assert mine["ok"] is True and "left off at step 3" in repr(mine)

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
