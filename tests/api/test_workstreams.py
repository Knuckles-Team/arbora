"""Workstream operations over the engine record (ARBORA-HARNESS-PLUGINS-R004)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import msgpack
import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import workstreams as ws
from arbora.api.registry import Confirm, Verb
from tests.harness_plugins.test_install_machine import described_edges


class Graph:
    """An in-memory engine: nodes, predecessors and the methods workstreams use."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[tuple[str, str]] = []
        self.sent: list[str] = []

    async def send(
        self, context: Any, method: str, params: dict[str, Any], **_: Any
    ) -> Any:
        self.sent.append(method)
        if method == "GetNodePropertiesBatch":
            return [
                (i, msgpack.packb(self.nodes[i]))
                for i in params["node_ids"]
                if i in self.nodes
            ]
        if method == "AddNode":
            self.nodes[params["node_id"]] = msgpack.unpackb(
                params["properties_msgpack"]
            )
            return None
        if method == "AddEdge":
            self.edges.append((params["source_id"], params["target_id"]))
            return None
        if method == "CompareAndSetNodeFields":
            node = self.nodes[params["node_id"]]
            for key, value in msgpack.unpackb(params["conditions_msgpack"]).items():
                assert node.get(key) == value
            node.update(msgpack.unpackb(params["updates_msgpack"]))
            return None
        if method == "GetPredecessors":
            return [s for s, t in self.edges if t == params["node_id"]]
        if method == "GetNodesByLabel":
            return [
                (i, msgpack.packb(n))
                for i, n in sorted(self.nodes.items())
                if params["label"] in n.get("labels", ())
            ]
        raise AssertionError(method)


def caller(principal: str, tenant: str = "tenant:a") -> Any:
    return SimpleNamespace(caller=SimpleNamespace(principal=principal, tenant=tenant))


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> Graph:
    graph = Graph()
    monkeypatch.setattr(ws, "engine_send", graph.send)
    return graph


OP = ws.operations()[0]
ATTACH = {
    "harness": "claude-code",
    "harness_session_id": "s1",
    "workspace_root": "/w/repo",
}


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R004.1")
def test_key_holds_digest_and_never_the_path() -> None:
    key = ws.workstream_key("tenant:a", "alice", "/w/repo")
    assert "/w/repo" not in key and key.startswith("workstream:tenant:a:")
    assert key != ws.workstream_key("tenant:a", "bob", "/w/repo")
    assert key != ws.workstream_key("tenant:b", "alice", "/w/repo")


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R004.2")
async def test_attach_is_idempotent_and_bounded(graph: Graph) -> None:
    alice = caller("alice")
    first = (await ws.handle_attach(alice, ATTACH, OP, clock_ms=lambda: 5))["value"]
    assert first["status"] == "attached"
    key = first["workstream"]
    assert all("/w/repo" not in repr(node) for node in graph.nodes.values())
    again = (await ws.handle_attach(alice, ATTACH, OP, clock_ms=lambda: 6))["value"]
    assert again["status"] == "already_attached" and again["context"] == {}
    assert len(graph.edges) == 1
    other = await ws.handle_attach(
        alice, {**ATTACH, "harness": "opencode", "harness_session_id": "s2"}, OP
    )
    assert other["value"]["context"]["attached_sessions"] == [
        "client_session:tenant:a:s1"
    ]
    assert len(repr(other["value"]["context"]).encode()) <= ws.CONTEXT_BYTES
    assert key in graph.nodes


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R004.3")
async def test_checkpoint_records_the_handoff_for_the_next_harness(
    graph: Graph,
) -> None:
    alice = caller("alice")
    await ws.handle_attach(alice, ATTACH, OP)
    done = await ws.handle_checkpoint(
        alice,
        {
            "harness_session_id": "s1",
            "workspace_root": "/w/repo",
            "summary": "left off at step 3",
            "run_ids": ["r1"],
            "trigger": "stop",
        },
        OP,
        clock_ms=lambda: 9,
    )
    assert done["value"]["status"] == "checkpointed"
    joined = await ws.handle_attach(
        alice, {**ATTACH, "harness": "opencode", "harness_session_id": "s2"}, OP
    )
    context = joined["value"]["context"]
    assert context["last_checkpoint"] == "left off at step 3"
    assert context["open_run_ids"] == ["r1"]
    detached = caller("alice")
    await ws.handle_detach(detached, {"harness_session_id": "s1"}, OP)
    with pytest.raises(IllegalCheckpoint):
        await ws.handle_checkpoint(
            alice,
            {"harness_session_id": "s1", "workspace_root": "/w/repo", "summary": "x"},
            OP,
        )


IllegalCheckpoint = ws.IllegalAttachTransition


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R004.4")
async def test_cross_principal_read_matches_unknown(graph: Graph) -> None:
    await ws.handle_attach(caller("alice"), ATTACH, OP)
    unknown = {"workspace_root": "/w/never"}
    errors = []
    for who, params in (
        (caller("bob"), {"workspace_root": "/w/repo"}),
        (caller("alice", "tenant:b"), {"workspace_root": "/w/repo"}),
        (caller("alice"), unknown),
    ):
        with pytest.raises(OperationRefused) as err:
            await ws.handle_get(who, params, OP)
        errors.append((err.value.code, err.value.details))
    assert errors == [("WORKSTREAM_NOT_FOUND", {})] * 3
    with pytest.raises(OperationRefused):
        await ws.handle_detach(caller("bob"), {"harness_session_id": "s1"}, OP)
    mine = await ws.handle_list(caller("alice"), {}, OP)
    assert len(mine["value"]["workstreams"]) == 1
    assert (await ws.handle_list(caller("bob"), {}, OP))["value"]["workstreams"] == []
    assert (await ws.handle_detach(caller("alice"), {"harness_session_id": "s1"}, OP))[
        "value"
    ]["status"] == "detached"
    again = await ws.handle_detach(caller("alice"), {"harness_session_id": "s1"}, OP)
    assert again["value"]["status"] == "detached"


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R004.5")
def test_attachment_edges_match_the_ontology() -> None:
    assert described_edges("WorkstreamAttachmentTransition") == {
        (s.value, e.value, t.value) for (s, e), t in ws.ATTACH_EDGES.items()
    }
    with pytest.raises(ws.IllegalAttachTransition):
        ws.transition(ws.AttachmentState.DETACHED, ws.AttachmentEvent.CHECKPOINT)


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R004.6")
def test_operations_and_scopes_are_registered() -> None:
    from arbora.api.ops.registry_factory import get_registry
    from arbora.security.scope_registry import SCOPE_CLASSES

    ops = {op.id: op for op in ws.operations()}
    assert set(ops) == {
        "workstream.attach",
        "workstream.checkpoint",
        "workstream.detach",
        "workstream.get",
        "workstream.list",
    }
    for op_id in ("workstream.attach", "workstream.checkpoint", "workstream.detach"):
        assert ops[op_id].verb is Verb.WRITE
        assert ops[op_id].confirm is Confirm.NONE
        assert ops[op_id].scopes == frozenset({"workstream:write"})
    assert ops["workstream.get"].verb is Verb.ASK
    assert ops["workstream.list"].verb is Verb.FIND
    registered = {op.id for op in get_registry()}
    assert set(ops) <= registered
    assert {"workstream:read", "workstream:write"} <= set(SCOPE_CLASSES)
