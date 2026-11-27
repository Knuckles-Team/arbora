"""P3: boundaries hold with several agents (ARBORA-ACCEPTANCE-R006.3).

Two agents, one human and one peer write one shared tenant namespace through the
served door under one policy. The engine double answers a graph query per verified
principal the way node-level visibility does, and the contradiction record is the
engine's ``main`` shape (a pin-lag guard names it until the re-pin).
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from arbora.pin_lag.unavailable import PinLagUnavailable
from arbora.security.actor_context import ActorType
from arbora.testing.scenario_stack import (
    CONTRADICTION_UNRECORDED,
    RESTRICTED_NODE_VISIBLE,
    ScenarioRefused,
    ScenarioRun,
    ScenarioStack,
    check_claim_lineage,
    check_contradiction,
    check_restricted_hidden,
)
from tests.api._served_support import McpAsk, person, route_methods
from tests.scenarios.conftest import scenario_stack  # noqa: F401
from tests.test_served_operation_api import _call

SHARED = ("kv:shared:write", "query:unified")
RESTRICTED = {"node:secret-1", "node:secret-2"}
OPEN = {"node:open-1"}


def _writer(name: str, kind: ActorType) -> Any:
    return dataclasses.replace(person(name, extra_scopes=SHARED), actor_type=kind)


def _claim(claim_id: str, text: str, actor: str) -> dict[str, Any]:
    return {
        "id": claim_id,
        "text": text,
        "source": "scenario",
        "actor": actor,
        "recorded_at": 5_000,
        "confidence": 0.9,
    }


def _graph_rows(_params: Any, principal: str) -> dict[str, Any]:
    """Node-level visibility: the engine answers the restricted principal without the subgraph."""
    visible = OPEN if principal == "person:limited" else OPEN | RESTRICTED
    return {"rows": [{"id": node} for node in sorted(visible)]}


@pytest.fixture
def people(scenario_stack: ScenarioStack, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    route_methods(
        scenario_stack.engine,
        {"Uql": _graph_rows},
        monkeypatch,
        {
            "fixture-verified": _writer("one", ActorType.HUMAN),
            "agent-a": _writer("agent-a", ActorType.AUTOMATED_SERVICE),
            "agent-b": _writer("agent-b", ActorType.AUTOMATED_SERVICE),
            "limited": person("limited", extra_scopes=("query:unified",)),
        },
    )


def _write(stack: ScenarioStack, bearer: str, claim: dict[str, Any], key: str) -> Any:
    return _call(
        stack.application,
        "memory.write",
        {"key": claim["id"], "scope": "shared", "value": claim},
        Authorization=bearer,
        **{"Idempotency-Key": key},
    )


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.3")
def test_shared_state_under_policy_with_contradiction(
    scenario_stack: ScenarioStack,  # noqa: F811
    people: None,
    invocation_capture: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = scenario_stack
    stack.require("application", "engine", "http", "mcp")
    peer_claim = _claim("claim-peer", "the window is open", "person:peer")
    claims = [
        _claim("claim-human", "the window is open", "person:one"),
        _claim("claim-agent-a", "the window is open", "person:agent-a"),
        _claim("claim-agent-b", "the window is closed", "person:agent-b"),
    ]
    assert (
        _write(stack, "Bearer fixture-verified", claims[0], "w-human").status_code
        == 200
    )
    # Agents and the peer reach the same door through the MCP verb surface.
    for name, claim in (
        ("agent-a", claims[1]),
        ("agent-b", claims[2]),
        ("peer", peer_claim),
    ):
        agent = McpAsk(
            invocation_capture["invocation"],
            monkeypatch,
            _writer(name, ActorType.AUTOMATED_SERVICE),
        )
        out = agent(
            "act",
            "memory.write",
            {"key": claim["id"], "scope": "shared", "value": claim},
            idempotency_key=f"w-{name}",
        )
        assert out["ok"], out

    stored = stack.http("memory.list", {"prefix": "claim-", "scope": "shared"})
    entries = [entry["value"] for entry in stored["entries"]]
    assert check_claim_lineage(entries) == 4

    admitted = _call(stack.application, "query.uql", {"text": "MATCH (n) RETURN n"})
    visible_open = {row["id"] for row in admitted.json()["result"]["rows"]}
    restricted = _call(
        stack.application,
        "query.uql",
        {"text": "MATCH (n) RETURN n"},
        Authorization="Bearer limited",
    )
    seen = {row["id"] for row in restricted.json()["result"]["rows"]}
    assert visible_open >= RESTRICTED
    check_restricted_hidden(seen, RESTRICTED)

    record = {
        "contradiction_id": "contradiction-1",
        "left": "claim-agent-a",
        "right": "claim-agent-b",
        "evidence": ["claim-agent-a", "claim-agent-b"],
        "status": "open",
        "detected_at_ms": 5_001,
    }
    check_contradiction(
        record, ("claim-agent-a", "claim-agent-b"), (e["id"] for e in entries)
    )
    run = ScenarioRun(
        scenario_id="p3-multi-agent",
        phase="P3",
        result="passed",
        commit="0" * 40,
        requirement_ids=("ARBORA-ACCEPTANCE-R006.3",),
    )
    assert run.engine_record()["record"]["phase"] == "P3"


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.3")
def test_a_leak_or_an_unrecorded_contradiction_fails_the_scenario() -> None:
    with pytest.raises(ScenarioRefused) as leak:
        check_restricted_hidden({"node:open-1", "node:secret-1"}, RESTRICTED)
    assert leak.value.code == RESTRICTED_NODE_VISIBLE
    assert leak.value.details == {"count": 1}

    pair = ("claim-agent-a", "claim-agent-b")
    record = {"left": pair[0], "right": pair[1]}
    with pytest.raises(ScenarioRefused) as none:
        check_contradiction(None, pair, pair)
    assert none.value.code == CONTRADICTION_UNRECORDED
    with pytest.raises(ScenarioRefused):
        check_contradiction({"left": pair[0], "right": "other"}, pair, pair)
    with pytest.raises(ScenarioRefused) as dropped:
        check_contradiction(record, pair, [pair[0]])
    assert dropped.value.code == CONTRADICTION_UNRECORDED

    # PIN-LAG: the pinned release lacks the engine's contradiction record shape.
    from arbora.pin_lag.medulla import require_models

    with pytest.raises(PinLagUnavailable) as lagging:
        require_models(("ContradictionRecord", "HiddenCount"))
    assert lagging.value.missing == "ContradictionRecord"
