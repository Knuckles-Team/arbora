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


_OFFER = {
    "blast_radius": "local",
    "cost_uncertainty_ppm": 0,
    "evidence_digests": ["sha256:e1"],
    "expected_cost_microunits": 10,
    "expected_utility_micros": 100,
    "probability_of_closure_ppm": 500_000,
    "reversible": True,
}


class _CallerEngine:
    """A caller-bound engine client; the lease belongs to principal ``p1``."""

    def __init__(self, principal: str, tenant: str = "t1") -> None:
        self.principal = principal
        self.tenant = tenant


@pytest.fixture
def leased(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def fake(client: Any, method: str, params: Any, graph: Any, **_: Any) -> Any:
        from epistemic_graph.generated import models

        calls.append((client.principal, method, params))
        if method == "GapGet":
            models.MethodGapGetParams.model_validate(params)
            return {"gap_id": params["gap_id"], "work_item_id": "w1"}
        if method == "MintWorkItemClaimCapability":
            models.MethodMintWorkItemClaimCapabilityParams.model_validate(params)
            if client.principal == "p1":
                return {"decision": "minted", "valid": True, "schema_version": "1"}
            return {"decision": "unauthorized", "valid": False, "schema_version": "1"}
        models.MethodWorkOfferPutParams.model_validate(params)
        return {"outcome": "applied", "offer_version": 1}

    monkeypatch.setattr("epistemic_graph.generated.send_by_id", fake)
    return calls


def _caller_context(services: Any, principal: str) -> Any:
    caller = SimpleNamespace(tenant="t1", principal=principal)
    return SimpleNamespace(
        caller=caller,
        services=services,
        idempotency_key="k",
        client=_CallerEngine(principal),
    )


@pytest.mark.spec("ARBORA-OPS-R022.1")
async def test_offer_is_served_only_to_the_bound_lease_holder(leased: Any) -> None:
    bound = _with_ops_runners({}, _Pool(), "tenant-graph", lambda t: t)
    (op,) = [o for o in work.operations() if o.id == "work.offers.create"]
    assert op.scopes == frozenset({"work:offer-write"})
    assert "item_id" not in op.params.model_fields
    params = op.params.model_validate(
        {"gap_id": "g1", "expected_offer_version": 0, "offer": _OFFER}
    ).model_dump()

    out = await work.handle_work(_caller_context(bound, "p1"), params, op)
    assert out["value"]["outcome"] == "applied"
    assert [(p, m) for p, m, _ in leased] == [
        ("p1", "GapGet"),
        ("p1", "MintWorkItemClaimCapability"),
        ("p1", "WorkOfferPut"),
    ]

    # A different principal in the same tenant cannot prove the lease: no offer.
    leased.clear()
    with pytest.raises(OperationRefused) as refused:
        await work.handle_work(_caller_context(bound, "p2"), params, op)
    assert refused.value.code == "PRINCIPAL_NOT_ALLOWED"
    assert [(p, m) for p, m, _ in leased] == [
        ("p2", "GapGet"),
        ("p2", "MintWorkItemClaimCapability"),
    ]


async def test_offer_requires_an_idempotency_key(leased: Any) -> None:
    bound = _with_ops_runners({}, _Pool(), "tenant-graph", lambda t: t)
    (op,) = [o for o in work.operations() if o.id == "work.offers.create"]
    context = _caller_context(bound, "p1")
    context.idempotency_key = None
    with pytest.raises(ValueError):
        await work.handle_work(
            context, {"gap_id": "g1", "expected_offer_version": 0, "offer": _OFFER}, op
        )
    assert leased == []
