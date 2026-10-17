"""Correlation headers cross agents and processes as one run."""

from __future__ import annotations

import pytest
from agent_connector_sdk.identity import ActorContext, ActorType, use_actor

from arbora.observability import correlation as c


def test_inject_carries_one_correlation_id_and_the_identity():
    actor = ActorContext(actor_id="alice", actor_type=ActorType.HUMAN, tenant_id="t1")
    with c.bind_carrier({c.CORRELATION_HEADER: "run-1", c.SESSION_HEADER: "s1"}):
        with use_actor(actor):
            headers = c.inject({"x-other": "kept"})
    assert headers["x-other"] == "kept"
    assert headers[c.CORRELATION_HEADER] == "run-1"
    assert headers[c.SESSION_HEADER] == "s1"
    assert headers[c.TENANT_HEADER] == "t1"
    assert headers[c.ACTOR_HEADER] == "alice"
    assert headers[c.TRACEPARENT_HEADER].startswith("00-")


def test_bind_carrier_restores_the_previous_context():
    with c.bind_carrier({c.CORRELATION_HEADER: "outer"}):
        with c.bind_carrier({c.CORRELATION_HEADER: "inner"}) as inner:
            assert inner == "inner"
        assert c.get_correlation_id() == "outer"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (
            {"X-Correlation-Id": "a", "Traceparent": "tp"},
            {c.CORRELATION_HEADER: "a", c.TRACEPARENT_HEADER: "tp"},
        ),
        ({"unrelated": "x"}, {}),
        (None, {}),
    ],
)
def test_extract_reads_the_carrier_case_insensitively(headers, expected):
    assert c.extract(headers) == expected


def test_carrier_round_trips_through_extract():
    with c.bind_carrier(
        {c.CORRELATION_HEADER: "run-2", c.PRIORITY_HEADER: "interactive"}
    ):
        carrier = c.current_carrier()
    assert c.extract(carrier)[c.PRIORITY_HEADER] == "interactive"
    with c.bind_carrier(c.extract(carrier)) as cid:
        assert cid == "run-2"
