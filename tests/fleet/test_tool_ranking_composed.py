"""The host composes the engine tool ranking behind tool search (ARBORA-OPS-R866)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
from arbora.fleet.multiplexer_ops import MultiplexerOps
from arbora.mcp_server.fleet_composition import tool_ranker_for
from tests.question_kinds import declare_question_kinds

_ROWS = tuple(
    CatalogItem(f"fleet:tool:s/{name}", "tool", name, description="a tool", server="s")
    for name in ("alpha", "beta", "gamma")
)


class _Engine:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.sent: list[tuple[str, dict[str, Any], str]] = []

    async def send(self, method: str, params: dict[str, Any], tenant: str) -> Any:
        self.sent.append((method, params, tenant))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


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
    engine = _Engine(
        {
            "ranked": [
                {"tool_id": "fleet:tool:s/gamma", "rank": 1},
                {"tool_id": "fleet:tool:s/alpha", "rank": 2},
            ]
        }
    )
    ranker = tool_ranker_for(engine.send)
    assert ranker is not None
    ops = _ops(ranker)
    found = await ops.find_tools(
        _caller(), query="tool", role="role:ops", context_budget_tokens=900
    )
    # The engine order leads; the tool it did not rank keeps its place after.
    assert [row["name"] for row in found["items"]] == ["gamma", "alpha", "beta"]
    [(method, params, tenant)] = engine.sent
    assert (method, tenant) == ("Decide", "tenant:one")
    question = params["request"]["question"]
    assert question["kind"] == "tool_relevance"
    assert question["task_text"] == "tool"
    assert question["agent_role_iri"] == "role:ops"
    assert question["context_budget_tokens"] == 900


@pytest.mark.spec("ARBORA-OPS-R866")
async def test_host_ranker_failure_refuses_unavailable_not_a_local_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.invoke.pipeline import OperationRefused

    declare_question_kinds(monkeypatch, "tool_relevance")
    ops = _ops(tool_ranker_for(_Engine(RuntimeError("down")).send))
    with pytest.raises(OperationRefused) as refused:
        await ops.find_tools(_caller(), query="alpha")
    assert refused.value.details == {"reason": "tool_ranking_unavailable"}
    malformed = _ops(tool_ranker_for(_Engine({"unranked": []}).send))
    with pytest.raises(OperationRefused):
        await malformed.find_tools(_caller(), query="alpha")


@pytest.mark.spec("ARBORA-OPS-R866")
def test_host_composes_no_ranker_until_the_contract_names_the_question() -> None:
    assert tool_ranker_for(_Engine({}).send) is None
    assert tool_ranker_for(None) is None
