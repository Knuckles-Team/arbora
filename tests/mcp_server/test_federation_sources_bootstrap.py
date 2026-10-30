"""Served federation sources keep ownership and grants in the engine (OPS-R019.1/.2).

The served bootstrap binds ``federation_sources`` to
:class:`EngineFederationSources`; every fact it reads or writes goes through
the engine's generic graph methods, faked here at ``send_by_id``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import msgpack
import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import federation
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Effect
from arbora.deployment.semantic_provisioning import CONTROL_GRAPH
from arbora.mcp_server.federation_sources import EngineFederationSources
from arbora.mcp_server.served_invocation import _with_federation_sources

_SOURCE = {"Http": {"url": "https://api.example", "token": "s3cret"}}


class _Engine:
    """Generic graph methods over dicts: nodes, directed edges, foreign sources."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: set[tuple[str, str]] = set()
        self.foreign: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []

    async def send(self, client: Any, method: str, params: Any, graph: Any, **_: Any):
        self.calls.append((method, graph))
        if method == "CreateNodeIfAbsent":
            if params["node_id"] in self.nodes:
                return False
            self.nodes[params["node_id"]] = msgpack.unpackb(
                params["properties_msgpack"]
            )
            return True
        if method == "GetNodeProperties":
            props = self.nodes.get(params["node_id"])
            return None if props is None else msgpack.packb(props)
        if method == "GetNodesByLabel":
            rows = sorted(
                (k, v) for k, v in self.nodes.items() if v["label"] == params["label"]
            )
            return [
                r for r in rows if params["after"] is None or r[0] > params["after"]
            ]
        if method == "AddEdge":
            self.edges.add((params["source_id"], params["target_id"]))
            return "ok"
        if method == "RemoveEdge":
            self.edges.discard((params["source_id"], params["target_id"]))
            return "ok"
        if method == "GetNeighbors":
            return [t for s, t in sorted(self.edges) if s == params["node_id"]]
        if method == "RegisterForeignSource":
            self.foreign[params["name"]] = params["source"]
            return params["name"]
        if method == "QueryForeignColumns":
            if params["name"] not in self.foreign:
                raise RuntimeError("unknown source")
            return {"columns": []}
        raise AssertionError(method)


class _Pool:
    async def connected_client(self, context: Any, graph: str) -> Any:
        return SimpleNamespace(graph=graph)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> _Engine:
    fake = _Engine()
    monkeypatch.setattr("epistemic_graph.generated.send_by_id", fake.send)
    return fake


def _served() -> dict[str, Any]:
    bound = _with_federation_sources({}, _Pool(), "tenant-graph", lambda t: t)
    assert isinstance(bound["federation_sources"], EngineFederationSources)
    return bound


_KEYS = iter(f"key-{n}" for n in range(1000))


async def _call(services: Any, op_id: str, who: str, **params: Any) -> Any:
    caller = SimpleNamespace(tenant="t1", principal=who)
    context = SimpleNamespace(
        caller=caller, services=services, idempotency_key=next(_KEYS)
    )
    op = next(o for o in federation.operations() if o.id == op_id)
    return (await federation.handle_federation(context, params, op))["value"]


def test_unbound_without_composition_is_not_bound() -> None:
    assert _with_federation_sources(None, _Pool(), "g", lambda t: t) is None


@pytest.mark.spec("ARBORA-OPS-R019.1")
async def test_register_records_owner_in_engine_and_probe_redacts(
    engine: _Engine,
) -> None:
    register = get_registry()["federation.sources.register"]
    assert register.effect is Effect.ADMIN
    assert register.idempotency.value == "key_required"
    assert register.audit.value == "event"
    served = _served()
    record = await _call(
        served, "federation.sources.register", "alice", source_id="s1", source=_SOURCE
    )
    assert record == {"source_id": "s1", "owner": "alice", "grantees": []}
    owner_node = engine.nodes["federation-source:t1:s1"]
    assert owner_node["owner"] == "alice" and "s3cret" not in str(owner_node)
    assert ("CreateNodeIfAbsent", CONTROL_GRAPH) in engine.calls
    assert ("RegisterForeignSource", "tenant-graph") in engine.calls
    listed = await _call(served, "federation.sources.list", "alice")
    assert [s["source_id"] for s in listed["sources"]] == ["s1"]
    got = await _call(served, "federation.sources.get", "alice", source_id="s1")
    assert got["owner"] == "alice"
    probe = await _call(served, "federation.sources.probe", "alice", source_id="s1")
    assert probe == {"source_id": "s1", "reachable": True}
    assert "s3cret" not in str(probe)
    with pytest.raises(OperationRefused):
        await _call(
            served,
            "federation.sources.register",
            "mallory",
            source_id="s1",
            source=_SOURCE,
        )
    assert engine.nodes["federation-source:t1:s1"]["owner"] == "alice"


@pytest.mark.spec("ARBORA-OPS-R019.2")
async def test_grants_are_engine_edges_and_revocation_blocks_next_probe(
    engine: _Engine,
) -> None:
    served = _served()
    await _call(
        served, "federation.sources.register", "alice", source_id="s1", source=_SOURCE
    )
    assert (await _call(served, "federation.sources.list", "bob"))["sources"] == []
    with pytest.raises(LookupError):
        await _call(served, "federation.sources.probe", "bob", source_id="s1")
    with pytest.raises(LookupError):
        await _call(
            served, "federation.sources.share", "bob", source_id="s1", grantee="bob"
        )
    shared = await _call(
        served, "federation.sources.share", "alice", source_id="s1", grantee="bob"
    )
    assert shared["grantees"] == ["bob"]
    assert ("federation-source:t1:s1", "federation-principal:t1:bob") in engine.edges
    assert len((await _call(served, "federation.sources.list", "bob"))["sources"]) == 1
    assert (await _call(served, "federation.sources.probe", "bob", source_id="s1"))[
        "reachable"
    ]
    assert (await _call(served, "federation.sources.list", "carol"))["sources"] == []
    with pytest.raises(PermissionError):
        await _call(
            served, "federation.sources.unshare", "bob", source_id="s1", grantee="bob"
        )
    await _call(
        served, "federation.sources.unshare", "alice", source_id="s1", grantee="bob"
    )
    assert not engine.edges
    with pytest.raises(LookupError):
        await _call(served, "federation.sources.probe", "bob", source_id="s1")
    # Re-sharing after a revoke writes a fresh edge (distinct idempotency key).
    await _call(
        served, "federation.sources.share", "alice", source_id="s1", grantee="bob"
    )
    assert (await _call(served, "federation.sources.get", "bob", source_id="s1"))[
        "grantees"
    ] == ["bob"]
