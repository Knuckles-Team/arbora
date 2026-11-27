"""The host composes the engine tool ranking behind tool search (ARBORA-OPS-R866)."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
from arbora.fleet.multiplexer_ops import MultiplexerOps
from arbora.fleet.tool_ranking import DEFAULT_BUDGET_TOKENS
from arbora.mcp_server.fleet_composition import tool_ranker_for
from tests._support import unwrap_decide
from tests.question_kinds import declare_question_kinds

_ROWS = tuple(
    CatalogItem(f"fleet:tool:s/{name}", "tool", name, description="a tool", server="s")
    for name in ("alpha", "beta", "gamma")
)


PRINCIPAL = "principal:host"
_PIN = {"feature_schema": "pin"}


def _advisory(*tool_ids: str) -> dict[str, Any]:
    scores = [
        {"option_id": tool, "score": {"scale": "q32", "value": 1 << 32}}
        for tool in tool_ids
    ]
    return {
        "outcome": "advisory",
        "calibrated": False,
        "ranked_total": 0,
        "scores": scores,
    }


class _Engine:
    """Answers one sealed record that echoes the request it was asked."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.sent: list[tuple[str, dict[str, Any], str]] = []

    async def send(self, method: str, params: dict[str, Any], tenant: str) -> Any:
        self.sent.append((method, params, tenant))
        params = unwrap_decide(params)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        digest = hashlib.sha256(PRINCIPAL.encode()).hexdigest()
        record = {
            "caller_principal": f"principal:sha256:{digest}",
            "tenant_id": params["tenant_id"],
            "question": params["question"],
            "inputs": {"params": params["params"]},
            "record_id": "r1",
            "record_digest": "d1",
            "outcome": self.outcome,
        }
        return {"records": [record]}

    def ranker(self) -> Any:
        return tool_ranker_for(self.send, lambda tenant: _PIN, lambda: PRINCIPAL)


def _ops(ranker: Any) -> MultiplexerOps:
    async def source():
        return _ROWS

    async def yes(_item, _caller):
        return True

    async def never(*_args):
        raise AssertionError("no child call")

    async def prime(_servers):
        return None

    return MultiplexerOps(
        catalog=FleetCatalog([source], yes),
        sessions=SimpleNamespace(pinned_items=lambda key: ()),  # type: ignore[arg-type]
        loadable=yes,
        mount=never,
        notify=never,
        invoke=never,
        health=lambda: {},
        tool_ranker=ranker,
    )


def _caller() -> Any:
    return SimpleNamespace(
        tenant="tenant:one",
        principal="alice",
        effective_scopes=frozenset({"mcp:discover"}),
    )


@pytest.mark.spec("ARBORA-OPS-R866")
async def test_host_ranker_orders_search_by_the_engine_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declare_question_kinds(monkeypatch, "tool_relevance")
    engine = _Engine(_advisory("fleet:tool:s/gamma", "fleet:tool:s/alpha"))
    ranker = engine.ranker()
    assert ranker is not None
    ops = _ops(ranker)
    found = await ops.find_tools(
        _caller(), query="tool", role="role:ops", context_budget_tokens=900
    )
    # The engine order leads; the tool it did not rank keeps its place after.
    assert [row["name"] for row in found["items"]] == ["gamma", "alpha", "beta"]
    [(method, params, tenant)] = engine.sent
    params = unwrap_decide(params)
    assert (method, tenant) == ("Decide", "tenant:one")
    _assert_asked(params, role="role:ops", budget=900)


def _assert_asked(params: dict[str, Any], *, role: str, budget: int) -> None:
    """The request is the engine's typed ``tool_assignment`` with role and budget."""
    assert params["question"]["kind"] == "tool_relevance"
    [typed] = params["params"]
    assert typed["name"] == "tool_assignment"
    assert typed["value"]["type"] == "tool_assignment"
    asked = typed["value"]["value"]
    assert asked["task_text"] == "tool"
    assert asked["agent_role_iri"] == role
    assert asked["context_budget_tokens"] == budget


@pytest.mark.spec("ARBORA-OPS-R866")
async def test_host_ranker_failure_refuses_unavailable_not_a_local_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.invoke.pipeline import OperationRefused

    declare_question_kinds(monkeypatch, "tool_relevance")
    ops = _ops(_Engine(RuntimeError("down")).ranker())
    with pytest.raises(OperationRefused) as refused:
        await ops.find_tools(_caller(), query="alpha")
    assert refused.value.details == {"reason": "tool_ranking_unavailable"}
    malformed = _ops(_Engine({"outcome": "acted"}).ranker())
    with pytest.raises(OperationRefused):
        await malformed.find_tools(_caller(), query="alpha")


@pytest.mark.spec("ARBORA-OPS-R866")
def test_host_composes_no_ranker_until_the_contract_names_the_question() -> None:
    assert _Engine({}).ranker() is None
    assert tool_ranker_for(None) is None


@pytest.mark.spec("ARBORA-OPS-R866")
async def test_a_budgetless_search_is_asked_with_the_configured_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declare_question_kinds(monkeypatch, "tool_relevance")
    monkeypatch.setenv("ARBORA_TOOL_SEARCH_CONTEXT_BUDGET_TOKENS", "1234")
    engine = _Engine(_advisory("fleet:tool:s/beta"))
    found = await _ops(engine.ranker()).find_tools(_caller(), query="beta")
    assert [row["name"] for row in found["items"]][0] == "beta"
    asked = unwrap_decide(engine.sent[0][1])["params"][0]["value"]["value"]
    # The role the engine requires is empty (the whole catalog); the budget is supplied.
    assert asked["agent_role_iri"] == "" and asked["context_budget_tokens"] == 1234
    monkeypatch.delenv("ARBORA_TOOL_SEARCH_CONTEXT_BUDGET_TOKENS")
    default = _Engine(_advisory())
    await _ops(default.ranker()).find_tools(_caller(), query="beta")
    budget = unwrap_decide(default.sent[0][1])["params"][0]["value"]["value"][
        "context_budget_tokens"
    ]
    assert budget == DEFAULT_BUDGET_TOKENS


@pytest.mark.spec("ARBORA-OPS-R866")
async def test_an_abstained_ranking_keeps_the_catalog_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declare_question_kinds(monkeypatch, "tool_relevance")
    engine = _Engine({"outcome": "abstained", "reasons": []})
    found = await _ops(engine.ranker()).find_tools(_caller(), query="tool")
    assert [row["name"] for row in found["items"]] == ["alpha", "beta", "gamma"]
