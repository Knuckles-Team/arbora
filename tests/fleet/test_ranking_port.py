"""The ranking port and the single lexical floor (ARBORA-FLEET-R083.1)."""

from __future__ import annotations

import pytest

from arbora.fleet.lexical_floor import LexicalFloor
from arbora.fleet.ranking import MedullaRanking, RankCandidate

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
        assert port.rank("q", QUERY, CATALOG) == floor_order
        assert (port.last_source, port.last_reason) == ("floor", reason)


@pytest.mark.spec("ARBORA-FLEET-R083.1")
def test_a_medulla_order_is_used_when_offered() -> None:
    seen = []

    def ask(question_id, query, candidates):
        seen.append((question_id, query, len(candidates)))
        return ["e", "ghost", "e", "a"]

    port = MedullaRanking(ask)
    order = port.rank("catalog.skill_rank", QUERY, CATALOG)
    assert order[:2] == ["e", "a"] and sorted(order) == ["a", "b", "c", "d", "e"]
    assert order[2:] == [
        i for i in MedullaRanking(None).rank("q", QUERY, CATALOG) if i not in {"e", "a"}
    ]
    assert seen == [("catalog.skill_rank", QUERY, 5)]
    assert port.last_source == "engine"
