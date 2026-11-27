"""The host composes one graph-backed port behind the city operations (ARBORA-FLEET-R084 to R088).

The engine has no spatial method, so the port reads and writes plain graph nodes through
the engine's existing methods. The bindings come from the host's own composition
function and each served call reaches an in-memory structural double of those methods.
"""

from __future__ import annotations

import asyncio
from typing import Any

import msgpack
import pytest

from arbora.api.invoke import OpError, OpResult, invoke
from arbora.api.registry import Surface
from arbora.mcp_server.served_invocation import _with_city_ports
from tests.fleet._city_support import caller, services_with

PUT = {"edits": [{"pin": "building:one", "cell": "2:2"}], "base_version": 0}


def _blob(props: dict[str, Any]) -> bytes:
    return msgpack.packb(props, use_bin_type=True)


class FakeGraph:
    """The five engine node methods the city port uses, over one dict of nodes."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.neighbours: dict[str, list[str]] = {}
        self.sent: list[tuple[str, dict[str, Any], bool]] = []

    async def __call__(
        self,
        context: Any,
        method: str,
        params: dict[str, Any],
        *,
        idempotent: bool = False,
    ) -> Any:
        self.sent.append((method, params, idempotent))
        return await getattr(self, "_" + method)(params)

    async def _GetNodesByLabel(self, params: dict[str, Any]) -> Any:
        rows = [
            (nid, _blob(p))
            for nid, p in sorted(self.nodes.items())
            if params["label"] in (p.get("labels") or [p.get("type")])
            and (params.get("after") is None or nid > params["after"])
        ]
        return rows[: params["limit"]]

    async def _GetNodePropertiesBatch(self, params: dict[str, Any]) -> Any:
        return [
            (nid, _blob(self.nodes[nid]) if nid in self.nodes else None)
            for nid in params["node_ids"]
        ]

    async def _GetNeighborsBatch(self, params: dict[str, Any]) -> Any:
        return [(n, self.neighbours.get(n, [])) for n in params["node_ids"]]

    async def _AddNode(self, params: dict[str, Any]) -> Any:
        props = msgpack.unpackb(params["properties_msgpack"], raw=False)
        self.nodes[params["node_id"]] = props

    async def _CompareAndSetNodeFields(self, params: dict[str, Any]) -> Any:
        node = self.nodes[params["node_id"]]
        wanted = msgpack.unpackb(params["conditions_msgpack"], raw=False)
        if any(node.get(k) != v for k, v in wanted.items()):
            return False
        node.update(msgpack.unpackb(params["updates_msgpack"], raw=False))
        return True


class FakeGateway:
    """The fleet gateway's census and one batched group read."""

    GROUPS = {"alpha": "ops", "beta": "ops", "zeta": "data"}

    def __init__(self) -> None:
        self.group_reads: list[tuple[str, ...]] = []
        self.item_reads = 0

    async def list(self, caller: Any, **params: Any) -> dict[str, Any]:
        if not params.get("summary"):
            self.item_reads += 1
            raise AssertionError("the layout reads no per-server item page")
        return {
            "servers": [{"server": "zeta"}, {"server": "alpha"}, {"server": "beta"}],
            "next_cursor": None,
        }

    def assert_one_batch(self) -> None:
        """One group read named every server, and no per-server page was read."""
        assert self.item_reads == 0
        assert [sorted(read) for read in self.group_reads] == [
            ["alpha", "beta", "zeta"]
        ]

    async def server_groups(self, caller: Any, servers: Any) -> dict[str, str]:
        self.group_reads.append(tuple(servers))
        return {name: self.GROUPS[name] for name in servers}


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> FakeGraph:
    fake = FakeGraph()
    monkeypatch.setattr("arbora.fleet.city_ports.engine_send", fake)
    return fake


def _call(op_id: str, params: dict[str, Any]) -> Any:
    return _call_with(op_id, params, FakeGateway())


def _call_with(op_id: str, params: dict[str, Any], gateway: FakeGateway) -> Any:
    bindings = _with_city_ports({"fleet_gateway": gateway})
    assert bindings is not None
    return asyncio.run(
        invoke(
            op_id,
            params,
            caller(),
            Surface.HTTP,
            services=services_with(dict(bindings)),
        )
    )


def test_the_host_binds_one_graph_port_to_the_four_city_services() -> None:
    from arbora.fleet import city_ports

    bindings = _with_city_ports({})
    assert bindings is not None
    assert set(bindings) == set(city_ports.CITY_SERVICES)
    assert {type(p) for p in bindings.values()} == {city_ports.GraphCityPort}
    assert _with_city_ports(None) is None
    own = object()
    assert _with_city_ports({"city_rules": own})["city_rules"] is own  # type: ignore[index]
    assert not hasattr(city_ports, "EngineWorldViewPort")


@pytest.mark.spec("ARBORA-FLEET-R084")
def test_the_layout_is_derived_client_side_with_stored_overrides(
    graph: FakeGraph,
) -> None:
    graph.nodes["world:override:alpha"] = {
        "type": "Building",
        "server_id": "alpha",
        "position_x": 7,
        "position_z": 9,
        "override_reason": "pinned by hand",
    }
    graph.nodes["world:override:gone"] = {
        "type": "Building",
        "server_id": "gone",
        "position_x": 1,
        "position_z": 1,
        "override_reason": "removed connector",
    }
    gateway = FakeGateway()
    result = _call_with("fleet.city.layout", {}, gateway)
    assert isinstance(result, OpResult) and result.code == "OK", result
    gateway.assert_one_batch()
    envelope = result.value["value"]
    assert [d["name"] for d in envelope["districts"]] == ["data", "ops"]
    by_id = {b["server_id"]: b for b in envelope["buildings"]}
    assert (by_id["zeta"]["x"], by_id["zeta"]["z"]) == (0, 0)
    assert (by_id["beta"]["x"], by_id["beta"]["z"]) == (220, 0)
    assert by_id["alpha"] | {"x": 7, "z": 9} == by_id["alpha"]
    assert by_id["alpha"]["overridden"] and by_id["alpha"]["reason"] == "pinned by hand"
    assert envelope["stale_overrides"] == ["gone"]
    assert {m for m, _p, _i in graph.sent} == {"GetNodesByLabel"}


@pytest.mark.spec("ARBORA-FLEET-R086")
def test_chunks_serve_an_anchor_neighbourhood_and_a_cell_range(
    graph: FakeGraph,
) -> None:
    graph.nodes["n:1"] = {"name": "anchor"}
    graph.nodes["n:2"] = {"name": "two"}
    graph.nodes["n:3"] = {"name": "three"}
    graph.neighbours["n:1"] = ["n:3", "n:2"]
    result = _call("fleet.city.chunk", {"anchor": "n:1"})
    assert isinstance(result, OpResult) and result.code == "OK", result
    chunk = result.value["value"]
    assert [n["id"] for n in chunk["nodes"]] == ["n:1", "n:2", "n:3"]
    assert chunk["nodes"][1]["provenance"] == {
        "tenant": "tenant:one",
        "node": "n:2",
        "source": "graph",
    }
    assert [e["to"] for e in chunk["edges"]] == ["n:2", "n:3"]
    assert chunk["next_cursor"] is None

    missing = _call("fleet.city.chunk", {"anchor": "n:404"})
    assert isinstance(missing, OpError) and missing.code == "NOT_FOUND"

    graph.nodes["b:in"] = {"type": "Building", "position_x": 5, "position_z": 5}
    graph.nodes["b:out"] = {"type": "Building", "position_x": 500, "position_z": 5}
    ranged = _call("fleet.city.chunk", {"cell_range": "0:0:10:10"})
    assert isinstance(ranged, OpResult) and ranged.code == "OK", ranged
    assert [n["id"] for n in ranged.value["value"]["nodes"]] == ["b:in"]
    bad = _call("fleet.city.chunk", {"cell_range": "10:10:0:0"})
    assert isinstance(bad, OpError) and bad.code == "INVALID_ARGUMENT"


@pytest.mark.spec("ARBORA-FLEET-R088")
def test_rules_serve_the_graph_registry_growth_and_seed(graph: FakeGraph) -> None:
    graph.nodes["rule:a"] = {"labels": ["CityArchetypeMapping"], "class": "Agent"}
    graph.nodes["rule:g"] = {"labels": ["CityGrowthRule"], "every": 3}
    graph.nodes["rule:s"] = {"labels": ["CitySeed"], "value": 42}
    result = _call("fleet.city.rules", {})
    assert isinstance(result, OpResult) and result.code == "OK", result
    value = result.value["value"]
    assert value["registry"] == [
        {"id": "rule:a", "labels": ["CityArchetypeMapping"], "class": "Agent"}
    ]
    assert value["growth_rules"][0]["every"] == 3
    assert value["seed"]["value"] == 42
    graph.nodes.clear()
    empty = _call("fleet.city.rules", {})
    assert isinstance(empty, OpResult)
    assert empty.value["value"] == {"registry": [], "growth_rules": [], "seed": None}


@pytest.mark.spec("ARBORA-FLEET-R087")
def test_the_edit_layer_writes_versions_idempotently_and_refuses_conflicts(
    graph: FakeGraph,
) -> None:
    first = _call("fleet.city.edit_layer.get", {})
    assert isinstance(first, OpResult)
    assert first.value["value"] == {"version": 0, "edits": [], "actor": None}

    from types import SimpleNamespace

    from arbora.api.ops import fleet_city
    from arbora.fleet.city_ports import GraphCityPort

    context = SimpleNamespace(
        services={"city_edit_layer": GraphCityPort()},
        caller=SimpleNamespace(tenant="tenant:one", principal="person:one"),
    )
    spec = next(
        o for o in fleet_city.operations() if o.id == "fleet.city.edit_layer.put"
    )

    def put(params: dict[str, Any]) -> Any:
        return asyncio.run(fleet_city.handle_edit_layer_put(context, params, spec))

    assert put(PUT)["value"] == {"version": 1, "actor": "person:one"}
    method, _params, idempotent = graph.sent[-1]
    assert (method, idempotent) == ("AddNode", True)
    second = put({"edits": [{"pin": "building:two", "cell": "3:3"}], "base_version": 1})
    assert second["value"]["version"] == 2
    method, _params, idempotent = graph.sent[-1]
    assert (method, idempotent) == ("CompareAndSetNodeFields", True)
    stored = graph.nodes["city:edit-layer:tenant:one"]
    assert (stored["version"], stored["actor"]) == (2, "person:one")

    from arbora.api.invoke.pipeline import OperationRefused

    with pytest.raises(OperationRefused) as stale:
        put({"edits": [{"pin": "x", "cell": "9:9"}], "base_version": 1})
    assert (stale.value.code, stale.value.details) == (
        "REVISION_CONFLICT",
        {"reason": "STALE_EDIT"},
    )
    with pytest.raises(OperationRefused) as collision:
        put(
            {
                "edits": [{"pin": "a", "cell": "1:1"}, {"pin": "b", "cell": "1:1"}],
                "base_version": 2,
            }
        )
    assert (collision.value.code, collision.value.details) == (
        "REVISION_CONFLICT",
        {"reason": "PIN_COLLISION"},
    )
    assert graph.nodes["city:edit-layer:tenant:one"]["version"] == 2
