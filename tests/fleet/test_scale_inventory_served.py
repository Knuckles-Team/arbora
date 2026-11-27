"""An operator sets the scaling inventory through one governed write (ARBORA-FLEET-R024.17).

The real invoke pipeline serves ``fleet.scale.inventory.set`` over HTTP with plan
confirmation; the engine is a structural double of its node reads and writes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import msgpack
import pytest

from arbora.fleet.scaling import graph_authority, inventory_writer
from arbora.fleet.scaling.graph_authority import INVENTORY
from tests.api._loopback_engine import loopback  # noqa: F401
from tests.fleet.scaling.test_scaling_authority_contract import authority
from tests.test_served_operation_api import served  # noqa: F401

OP = "fleet.scale.inventory.set"


class Nodes:
    """The engine's node store: reads by class, keyed writes, compare-and-set."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.keys: set[str] = set()
        self.writes: list[tuple[str, str]] = []

    async def read(self, context: Any, method: str, params: dict[str, Any]) -> Any:
        assert method == "GetNodesByLabel"
        rows = sorted(
            (nid, props)
            for nid, props in self.nodes.items()
            if params["label"] in props["labels"]
            and (params.get("after") is None or nid > params["after"])
        )
        return [
            (nid, msgpack.packb(props, use_bin_type=True))
            for nid, props in rows[: params["limit"]]
        ]

    async def write(
        self,
        client: Any,
        method: str,
        params: dict[str, Any],
        graph: str,
        *,
        key: str | None,
    ) -> Any:
        assert key, "every inventory write is keyed"
        if key in self.keys:
            return True
        self.keys.add(key)
        node = params["node_id"]
        self.writes.append((method, node))
        if method == "AddNode":
            self.nodes[node] = msgpack.unpackb(params["properties_msgpack"], raw=False)
        elif method == "RemoveNode":
            del self.nodes[node]
        else:
            assert method == "CompareAndSetNodeFields"
            conditions = msgpack.unpackb(params["conditions_msgpack"], raw=False)
            updates = msgpack.unpackb(params["updates_msgpack"], raw=False)
            held = self.nodes[node]
            if any(held.get(k) != v for k, v in conditions.items()):
                return False
            held.update(updates)
        return True


def _params(snapshot: Any, base: int = 0, **over: Any) -> dict[str, Any]:
    body = {
        field: [r.model_dump(mode="json") for r in getattr(snapshot, field)]
        for field, _label, _model in INVENTORY
    }
    return {"base_revision": base, **body, **over}


@pytest.fixture
def nodes(monkeypatch: pytest.MonkeyPatch) -> Nodes:
    store = Nodes()
    monkeypatch.setattr(graph_authority, "engine_send", store.read)
    monkeypatch.setattr(inventory_writer, "send_on_client", store.write)
    return store


@pytest.fixture
def operator(loopback: Any) -> Any:  # noqa: F811
    loopback.serve({}, "mcp:admin")
    return loopback


@pytest.mark.spec("ARBORA-FLEET-R024.17")
def test_an_operator_sets_the_inventory_through_invoke_and_the_reader_sees_it(
    operator: Any, nodes: Nodes
) -> None:
    snapshot = authority()
    base = _params(snapshot)
    spare = {**base["failure_domains"][0], "domain_id": "host:spare"}
    params = {**base, "failure_domains": [*base["failure_domains"], spare]}
    shown = operator.call(OP, params, "inv-1")
    assert shown.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert nodes.writes == []

    done = operator.value(operator.confirmed(OP, params, "inv-1"))
    assert done["authority_revision"] == 1 and done["added"] == 6
    assert {method for method, _ in nodes.writes} == {"AddNode"}
    assert nodes.writes[-1] == ("AddNode", inventory_writer.REVISION_NODE)
    seen = asyncio.run(graph_authority.GraphScaleAuthority().snapshot(object()))
    assert seen.authority_revision == 1
    assert {d.domain_id for d in seen.failure_domains} == {
        snapshot.failure_domains[0].domain_id,
        "host:spare",
    }
    for field in ("pools", "workloads", "units", "controllers"):
        assert getattr(seen, field) == getattr(snapshot, field)

    # A second set changes one pool and drops the spare domain.
    pool = {**base["pools"][0], "capacity_units": 20}
    again = {**base, "base_revision": 1, "pools": [pool]}
    nodes.writes.clear()
    moved = operator.value(operator.confirmed(OP, again, "inv-2"))
    assert moved["authority_revision"] == 2
    assert (moved["changed"], moved["removed"], moved["unchanged"]) == (1, 1, 4)
    methods = [method for method, _ in nodes.writes]
    assert methods == [
        "CompareAndSetNodeFields",
        "RemoveNode",
        "CompareAndSetNodeFields",
    ]
    assert nodes.writes[-1][1] == inventory_writer.REVISION_NODE


@pytest.mark.spec("ARBORA-FLEET-R024.17")
def test_a_stale_base_revision_and_an_invalid_inventory_write_nothing(
    operator: Any, nodes: Nodes
) -> None:
    snapshot = authority()
    stale = operator.confirmed(OP, _params(snapshot, base=3), "inv-stale")
    assert stale.json()["error"]["code"] == "REVISION_CONFLICT", stale.text
    dangling = _params(snapshot, units=[])
    invalid = operator.confirmed(OP, dangling, "inv-invalid")
    assert invalid.json()["error"]["code"] == "INVALID_ARGUMENT", invalid.text
    assert nodes.writes == [] and nodes.nodes == {}


@pytest.mark.spec("ARBORA-FLEET-R024.17")
def test_a_replay_of_the_confirmed_plan_writes_nothing_twice(
    operator: Any, nodes: Nodes
) -> None:
    params = _params(authority())
    ref = operator.preview(OP, params, "inv-replay")
    first = operator.call(OP, params, "inv-replay", ref)
    assert first.status_code == 200, first.text
    written = list(nodes.writes)
    replay = operator.call(OP, params, "inv-replay", ref)
    assert replay.status_code in {200, 400, 409}
    assert nodes.writes == written
