"""ARBORA-FLEET-R083.2: intent capability and action order comes from the ranking port."""

from __future__ import annotations

import pytest

from arbora.mcp_server.backing.tools import intent_tools
from arbora.mcp_server.host_caches import HostCaches


class _Recording:
    """A port that records each question and answers a fixed reversal of the floor."""

    configured = True

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, list[str]]] = []

    def rank(self, question_id, query, candidates):
        ids = [c.id for c in candidates]
        self.asked.append((question_id, query, ids))
        return sorted(ids, reverse=True)


@pytest.mark.spec("ARBORA-FLEET-R083.2")
def test_intent_actions_are_ordered_by_the_ranking_port() -> None:
    port = _Recording()
    tool = next(
        t for t, actions in intent_tools._actions_by_tool().items() if len(actions) > 2
    )
    actions = intent_tools._actions_by_tool()[tool]

    ordered = [a for a, _ in intent_tools._order_actions(tool, "anything", port)]

    assert ordered == sorted(actions, reverse=True)
    assert port.asked[0][0] == "catalog.intent_action_rank"
    assert sorted(port.asked[0][2]) == sorted(actions)


@pytest.mark.spec("ARBORA-FLEET-R083.2")
def test_resolved_capabilities_follow_the_port_and_carry_floor_evidence() -> None:
    port = _Recording()
    resolved = intent_tools.resolve_intent(
        HostCaches(), None, "search the graph", top_k=5, ranking=port
    )

    assert [c.capability_id for c in resolved] == sorted(
        (c.capability_id for c in resolved), reverse=True
    )
    assert port.asked and port.asked[0][0] == "catalog.intent_action_rank"
    # No bonus survives: nothing but the port's order and the floor's evidence.
    assert not hasattr(intent_tools, "_skill_delegation_bonus")
    assert not hasattr(intent_tools, "_declared_action_phrase_bonus")
