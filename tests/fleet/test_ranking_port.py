"""The ranking port and the single lexical floor (ARBORA-FLEET-R083.1)."""

from __future__ import annotations

import pytest

from arbora.api.domain.fleet.lexical_floor import LexicalFloor
from arbora.api.domain.fleet.ranking import MedullaRanking, RankCandidate
from arbora.mcp_server.host_caches import HostCaches

CATALOG = [
    RankCandidate("a", "media_list_all", "list media in the library"),
    RankCandidate("b", "seerr_get_requests", "list pending media requests from seerr"),
    RankCandidate("c", "seerr_get_request_count", "count of requests in seerr"),
    RankCandidate("d", "arr_list_queue", "list the download queue"),
    RankCandidate("e", "unrelated_thing", "nothing here"),
]
QUERY = "list pending media requests in seerr"


@pytest.mark.spec("ARBORA-FLEET-R083.1")
def test_floor_orders_like_the_r074_ranker() -> None:
    floor = LexicalFloor(QUERY, (f"{c.name} {c.description}" for c in CATALOG))
    scored = sorted(
        range(len(CATALOG)),
        key=lambda i: -floor.score(CATALOG[i].name, CATALOG[i].description),
    )
    order = MedullaRanking(None).rank("catalog.tool_rank", QUERY, CATALOG)
    assert order[:2] == ["b", "c"] and order[-1] == "e"
    assert [CATALOG[i].id for i in scored][:2] == order[:2]
    assert order == MedullaRanking(None).rank("catalog.tool_rank", QUERY, CATALOG)


@pytest.mark.spec("ARBORA-FLEET-R083.1")
def test_abstention_error_and_timeout_answer_the_floor_order() -> None:
    floor_order = MedullaRanking(None).rank("q", QUERY, CATALOG)

    def abstain(*_args):
        return None

    def broken(*_args):
        raise RuntimeError("engine down")

    def slow(*_args):
        raise TimeoutError

    for ask, reason in (
        (abstain, "abstained"),
        (broken, "decision_unavailable"),
        (slow, "timeout"),
    ):
        port = MedullaRanking(ask)
        answer = port.ordered("q", QUERY, CATALOG)
        assert answer.order == floor_order
        assert (answer.source, answer.reason) == ("floor", reason)


@pytest.mark.spec("ARBORA-FLEET-R083.1")
def test_a_medulla_order_is_used_when_offered() -> None:
    seen = []

    def ask(question_id, query, candidates):
        seen.append((question_id, query, len(candidates)))
        return ["e", "ghost", "e", "a"]

    port = MedullaRanking(ask)
    answer = port.ordered("catalog.skill_rank", QUERY, CATALOG)
    order = answer.order
    assert order[:2] == ["e", "a"] and sorted(order) == ["a", "b", "c", "d", "e"]
    assert order[2:] == [
        i for i in MedullaRanking(None).rank("q", QUERY, CATALOG) if i not in {"e", "a"}
    ]
    assert seen == [("catalog.skill_rank", QUERY, 5)]
    assert answer.source == "medulla"


class _Recording:
    configured = True

    def __init__(self) -> None:
        self.questions: list[str] = []

    def rank(self, question_id, query, candidates):
        self.questions.append(question_id)
        return [c.id for c in candidates]


@pytest.mark.spec("ARBORA-FLEET-R083")
async def test_every_ranking_site_asks_the_port_and_falls_back_to_the_floor() -> None:
    """Each site asks its own question of the port; unbound, each answers the floor."""
    from arbora.api.mcp.resolve import Descriptor, IntentResolver
    from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
    from arbora.mcp_server.backing.connector_surface import (
        Operation,
        order_operations,
    )
    from arbora.mcp_server.backing.tools import intent_tools

    port = _Recording()
    descriptors = [
        Descriptor(id="deploy.restart", verb="act", summary="restart a service"),
        Descriptor(id="deploy.status", verb="act", summary="deployment status"),
    ]
    operations = {
        "restart": Operation("restart", "svc", None, "restart a service", False),
        "status": Operation("status", "svc", None, "service status", True),
    }
    items = [
        CatalogItem(
            id="i:1",
            kind="tool",
            name="restart",
            description="restart service",
            server="svc",
        )
    ]

    async def source() -> list[CatalogItem]:
        return items

    async def visible(_item, _caller) -> bool:
        return True

    class _Caller:
        effective_scopes = frozenset({"mcp:discover"})

    catalog = FleetCatalog((source,), visible, ranking=port)
    await catalog.search(_Caller(), query="restart")
    IntentResolver(ranking=port).arrange("act", "restart", descriptors, scope_ref=None)
    order_operations(operations, "restart service", ranking=port)
    intent_tools.resolve_intent(HostCaches(), None, "search the graph", ranking=port)

    assert port.questions == [
        "catalog.catalog_item_rank",
        "catalog.operation_rank",
        "catalog.operation_rank",
        "catalog.intent_action_rank",
    ]
    # Unbound, each site answers without the engine and fails nothing.
    unbound = FleetCatalog((source,), visible)
    assert (await unbound.search(_Caller(), query="restart"))["items"]
    assert IntentResolver().arrange("act", "restart", descriptors, scope_ref=None)
    assert order_operations(operations, "restart service")
    assert intent_tools.resolve_intent(HostCaches(), None, "search the graph")
