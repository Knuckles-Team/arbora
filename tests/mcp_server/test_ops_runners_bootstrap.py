"""The served bootstrap binds the decide and work runners (OPS-R021.1, R022.1)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import decide, work
from arbora.mcp_server.ops_runners import EngineDecideRunner, EngineWorkRunner
from arbora.mcp_server.served_invocation import _with_ops_runners


class _Pool:
    def __init__(self) -> None:
        self.graphs: list[str] = []

    async def connected_client(self, context: Any, graph: str) -> Any:
        self.graphs.append(graph)
        return SimpleNamespace(graph=graph)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any], str]]:
    calls: list[tuple[str, dict[str, Any], str]] = []

    async def fake(client: Any, method: str, params: Any, graph: Any, **_: Any) -> Any:
        calls.append((method, params, graph))
        if method == "GetWorkItem":
            return {"work_item_id": params["work_item_id"]}
        if method == "ListWorkItems":
            return {"items": [], "next_cursor": None}
        return {"decision_id": "d1"}

    monkeypatch.setattr("epistemic_graph.generated.send_by_id", fake)
    return calls


def _context(services: Any) -> Any:
    caller = SimpleNamespace(tenant="t1", principal="p1")
    return SimpleNamespace(caller=caller, services=services, idempotency_key="k")


def test_unbound_without_composition_is_not_bound() -> None:
    assert _with_ops_runners(None, _Pool(), "g", lambda t: t) is None


@pytest.mark.spec("ARBORA-OPS-R021.1")
async def test_decide_commit_is_served_from_the_bootstrap_binding(sent: Any) -> None:
    bound = _with_ops_runners({}, _Pool(), "tenant-graph", lambda t: t)
    assert isinstance(bound["decide_runner"], EngineDecideRunner)
    (op,) = decide.operations()
    out = await decide.handle_decide_commit(
        _context(bound), {"request": {"record": {}}}, op
    )
    assert out["value"] == {"decision_id": "d1"}
    assert out["provenance"]["op"] == "decide.commit"
    assert sent == [("DecisionCommit", {"request": {"record": {}}}, "tenant-graph")]


async def test_work_reads_are_served_from_the_bootstrap_binding(sent: Any) -> None:
    bound = _with_ops_runners({}, _Pool(), "tenant-graph", lambda t: t)
    assert isinstance(bound["work_runner"], EngineWorkRunner)
    ops = {o.id: o for o in work.operations()}
    listed = await work.handle_work(
        _context(bound), {"limit": 5}, ops["work.items.list"]
    )
    assert listed["value"]["items"] == []
    got = await work.handle_work(
        _context(bound), {"item_id": "w1"}, ops["work.items.get"]
    )
    assert got["value"] == {"work_item_id": "w1"}
    assert [c[0] for c in sent] == ["ListWorkItems", "GetWorkItem"]
    with pytest.raises(OperationRefused) as refused:
        await work.handle_work(
            _context(bound), {"item_id": "w1", "terms": {}}, ops["work.offers.create"]
        )
    assert refused.value.code == "UNAVAILABLE"
