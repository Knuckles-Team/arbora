"""The orchestration probe questions through the intent tools (ARBORA-FLEET-R052.4).

Each probe asks over the served MCP intent surface as the verified person against the
deployed-shape fixture of ``tests/fleet/_probe_fixture.py``: the real fleet gateway and
the recording engine. A probe passes when the answer is non-empty, matches the stated
content and cites the engine record it read. The receipt each probe writes is the
evidence the medulla companion row cites.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.fleet._probe_fixture import (
    CONTAINER_TOOLS,
    RECORD,
    ProbeStack,
    build_probe_stack,
    install_fleet,
)
from tests.test_served_operation_api import served  # noqa: F401

COUNTS = [{"label": "Agent", "count": 5}, {"label": "Tool", "count": 7}]
STEPS = [
    {"step": 1, "tool": "pull_image"},
    {"step": 2, "tool": "create_container"},
    {"step": 3, "tool": "start_container"},
]
SPEC_NODES = [
    {"node": "spec:ARBORA-FLEET-R029", "digest": "sha256:aa"},
    {"node": "doc:ecosystem-boundaries", "digest": "sha256:bb"},
]


@pytest.fixture
def holder(monkeypatch: pytest.MonkeyPatch) -> Any:
    return install_fleet(monkeypatch)


def _stack(
    holder: Any,
    pair: Any,
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[str, Any],
) -> ProbeStack:
    return build_probe_stack(monkeypatch, pair, holder, answers)


def _answer(reply: dict[str, Any], op: str) -> dict[str, Any]:
    """The served answer; an empty one fails the probe as ``EMPTY_ANSWER``."""
    assert reply.get("ok") is True, reply
    assert reply["meta"]["resolved_op"] == op, reply["meta"]
    body = reply["result"]
    assert body.get("rows"), "EMPTY_ANSWER"
    assert RECORD in body["premises"], "the answer cites no engine record"
    return body


def _receipt(tmp_path: Path, probe: str, question: str, body: dict[str, Any]) -> None:
    path = tmp_path / f"{probe}.json"
    path.write_text(json.dumps({"probe": probe, "question": question, "answer": body}))
    assert json.loads(path.read_text())["answer"]["premises"] == body["premises"]


@pytest.mark.spec("ARBORA-FLEET-R052.4.1")
def test_probe_node_counts_by_label(holder, served, monkeypatch, tmp_path) -> None:  # noqa: F811
    question = "How many nodes does the graph hold, by label?"
    answers = {
        "NlQuery": {"rows": COUNTS, "premises": [RECORD]},
        "Sparql": {"rows": COUNTS},
    }
    stack = _stack(holder, served, monkeypatch, answers)
    body = _answer(stack("ask", question), "query.nl")
    direct = stack.op(
        "ask", "query.sparql", {"query": "SELECT ?label (COUNT(?n) AS ?count)"}
    )
    assert direct["ok"] is True, direct
    assert body["rows"] == direct["result"]["rows"]
    assert sum(row["count"] for row in body["rows"]) == 12
    _receipt(tmp_path, "node-counts", question, body)


@pytest.mark.spec("ARBORA-FLEET-R052.4.2")
def test_probe_deploy_container_plan(holder, served, monkeypatch, tmp_path) -> None:  # noqa: F811
    question = "How can I deploy a container with container-manager?"
    stack = _stack(
        holder, served, monkeypatch, {"NlQuery": {"rows": STEPS, "premises": [RECORD]}}
    )
    body = _answer(stack("ask", question), "query.nl")
    assert len(body["rows"]) >= 2
    catalog = stack.op("find", "fleet.catalog.list", {"servers": ["container-manager"]})
    listed = {item["name"] for item in catalog["result"]["value"]["items"]}
    assert listed == set(CONTAINER_TOOLS)
    assert {row["tool"] for row in body["rows"]} <= listed
    _receipt(tmp_path, "deploy-container", question, body)


@pytest.mark.spec("ARBORA-FLEET-R052.4.4")
def test_probe_why_intent_surface(holder, served, monkeypatch, tmp_path) -> None:  # noqa: F811
    question = "Why does Arbora use the intent surface?"
    stack = _stack(
        holder,
        served,
        monkeypatch,
        {"NlQuery": {"rows": SPEC_NODES, "premises": [RECORD]}},
    )
    body = _answer(stack("ask", question), "query.nl")
    assert any("ARBORA-FLEET-R029" in row["node"] for row in body["rows"])
    _receipt(tmp_path, "why-intent-surface", question, body)


@pytest.mark.spec("ARBORA-FLEET-R052.4.5")
def test_probe_find_list_containers_tool(holder, served, monkeypatch, tmp_path) -> None:  # noqa: F811
    question = "find a tool that lists docker containers"
    stack = _stack(holder, served, monkeypatch, {})
    found = stack("find", question, {"limit": 3})
    items = found["result"]["items"]
    assert items, "EMPTY_ANSWER"
    assert items[0]["id"] == "fleet:tool:container-manager/list_containers"
    assert all(item.get("name") != "send_email" for item in items)
    (tmp_path / "find.json").write_text(
        json.dumps({"question": question, "top": items})
    )


@pytest.mark.spec("ARBORA-FLEET-R052.4.3")
def test_probe_agent_library_listing() -> None:
    """The Agent Library question routes to the engine's own read, not the fleet catalog."""
    from arbora.api.mcp.resolve import IntentResolver

    library = {
        "op": "eg.storage.AgentLibrary",
        "verb": "ask",
        "summary": "Read the tenant Agent Library",
        "params_schema": {"required": ["op"]},
    }
    catalog = {
        "op": "fleet.catalog.list",
        "verb": "ask",
        "summary": "List agents tools and prompts in the fleet catalog",
        "params_schema": {},
    }
    question = "Which agents are in the Agent Library?"
    resolver = IntentResolver()
    routed = resolver.resolve("ask", question, [catalog, library], scope_ref="s")
    assert routed.op == "eg.storage.AgentLibrary"
    assert routed.missing_required == ("op",)
    assert routed.preview is False

    supplied = {"op": {"operation": "current", "agent_id": "agent:a"}}
    filled = resolver.resolve(
        "ask", question, [catalog, library], scope_ref="s", params=supplied
    )
    assert filled.op == "eg.storage.AgentLibrary" and filled.missing_required == ()

    # No authorized AgentLibrary read means no route: the caller lacks the scope
    # and the fleet catalog never stands in for it.
    hidden = resolver.resolve("ask", question, [catalog], scope_ref="s")
    assert hidden.op != "eg.storage.AgentLibrary"


@pytest.mark.spec("ARBORA-FLEET-R052.4.6")
def test_probe_delegate_list_containers(holder, served, monkeypatch, tmp_path) -> None:  # noqa: F811
    question = "Delegate: list running containers"
    stack = _stack(holder, served, monkeypatch, {})
    call = {"server": "container-manager", "tool": "list_containers", "arguments": {}}
    reply = stack("act", question, call)
    assert reply.get("ok") is True, reply
    assert reply["meta"].get("resolved_op", "fleet.call") == "fleet.call"
    containers = reply["result"]["value"]["structuredContent"]["containers"]
    assert containers, "EMPTY_ANSWER"
    assert containers == [{"id": "c1", "image": "nginx", "state": "running"}]
    assert stack.child_calls == [("container-manager", "list_containers", {})]
    _receipt(
        tmp_path, "delegate-containers", question, {"premises": [], "rows": containers}
    )
