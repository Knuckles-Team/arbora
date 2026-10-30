"""Unqualified historical audit candidates cannot look like unused budget."""

import asyncio

import pytest

from arbora.access.action_policy import ActionPolicy, ActionRequest, LedgerUnreadable
from tests.security.test_anonymous_mode import local_mode as local_mode
from tests.test_served_engine_native_labels import native_engine as native_engine

pytestmark = pytest.mark.spec("ARBORA-FLEET-R015")


def test_native_type_only_history_refuses_rate_and_blast_accounting(native_engine):
    engine, session = native_engine
    client = engine.graph_compute.for_graph(session.graph).async_client
    assert asyncio.run(
        client.nodes.create_if_absent(
            "legacy-decision",
            {
                "id": "legacy-decision",
                "type": "ActionDecision",
                "kind": "observe",
                "target": "legacy-target",
            },
        )
    )
    policy = ActionPolicy(engine)
    request = ActionRequest(
        kind="observe", target="legacy-target", source="qualification"
    )
    assert isinstance(policy._recent_decisions("observe"), LedgerUnreadable)
    assert policy._rate_exceeded(request, {"max": 1, "window_s": 3600})
    assert policy._blast_exceeded(request, {"max_targets": 1, "window_s": 3600})
    assert engine.query_cypher(
        "MATCH (n {id: 'legacy-decision'}) RETURN n.node_type AS label"
    ) == [{"label": None}]
    assert policy._recent_decisions("unrelated-kind") == []
