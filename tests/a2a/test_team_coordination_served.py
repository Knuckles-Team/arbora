"""Team coordination writes records and applies the decision (ARBORA-OPS-R877)."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import msgpack
import pytest

from arbora.a2a.models import A2AMessage, A2ARouteDecision, A2ATextPart
from arbora.a2a.team import TeamCoordinator, bind_team_coordination
from arbora.security.session import use_session
from tests.a2a.test_checkpoint_resume_served import Host, _world, lenient_decode
from tests.a2a.test_operation_bridge import session
from tests.question_kinds import declare_question_kinds

__all__ = ["lenient_decode"]


PRINCIPAL = "principal:host"
_PIN = {"feature_schema": "pin"}
_ONE = {"scale": "q32", "value": 1 << 32}
_ZERO = {"scale": "q32", "value": 0}


def _abstained() -> dict[str, Any]:
    reason = {"reason": "family", "code": "TEAM_SIGNAL_STALE"}
    return {"outcome": "abstained", "reasons": [reason]}


def _advisory(down: bool) -> dict[str, Any]:
    """The sealed advisory outcome: the concluded option scores one."""
    scores = [
        {"option_id": "primary_channel", "score": _ZERO if down else _ONE},
        {"option_id": "team_coordination", "score": _ONE if down else _ZERO},
    ]
    return {
        "outcome": "advisory",
        "calibrated": False,
        "ranked_total": 2,
        "scores": scores,
    }


def _signal(request: dict[str, Any]) -> dict[str, Any]:
    """The typed ``ChannelSignal`` value of one team question."""
    typed = {p["name"]: p["value"]["value"] for p in request["params"]}
    return typed["channel_signal"]


class _Graph:
    """The engine graph and decision behind the host service send."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.abstain = False

    async def send(self, method: str, params: dict[str, Any], tenant: str) -> Any:
        if method == "CreateNodeIfAbsent":
            props = msgpack.unpackb(params["properties_msgpack"], raw=False)
            self.nodes.setdefault(params["node_id"], props)
            return {}
        if method == "CompareAndSetNodeFields":
            updates = msgpack.unpackb(params["updates_msgpack"], raw=False)
            self.nodes[params["node_id"]].update(updates)
            return {}
        assert method == "Decide"
        return self._decide(params)

    def _decide(self, params: dict[str, Any]) -> Any:
        self.requests.append(params)
        outcome = (
            _abstained()
            if self.abstain
            else _advisory(not _signal(params)["available"])
        )
        digest = hashlib.sha256(PRINCIPAL.encode()).hexdigest()
        record = {
            "caller_principal": f"principal:sha256:{digest}",
            "tenant_id": params["tenant_id"],
            "question": params["question"],
            "inputs": {"params": params["params"]},
            "record_id": "r1",
            "record_digest": "d1",
            "outcome": outcome,
        }
        return {"records": [record]}


def _coordinator(graph: _Graph) -> TeamCoordinator:
    return TeamCoordinator.over_host(
        graph.send, "tenant:one", graph="g", feature_schema=_PIN, principal=PRINCIPAL
    )


@pytest.mark.spec("ARBORA-OPS-R877")
async def test_unavailable_starts_team_available_keeps_primary_and_status_survives_restart() -> (
    None
):
    graph = _Graph()
    args = {
        "team_id": "t1",
        "members": ["a", "b"],
        "tasks": {"k1": "submitted"},
        "observed_at_ms": 5,
    }
    started = await _coordinator(graph).coordinate(channel_signal="unavailable", **args)
    assert started["path"] == "team_coordination"
    labels = {n["label"] for n in graph.nodes.values()}
    assert labels == {"Team", "TeamMember", "TeamTask"}
    # The graph shape the engine reads: the team marker and each member bound to it.
    assert graph.nodes["team:t1"]["_team"] is True
    member = graph.nodes["team:t1:member:a"]
    assert member["_team_member"] is True and member["boundToTeam"] == "team:t1"
    request = graph.requests[0]
    assert request["question"]["kind"] == "team_coordination"
    typed = {p["name"]: p["value"] for p in request["params"]}
    assert typed["graph"] == {"type": "text", "value": "g"}
    assert typed["team"] == {"type": "text", "value": "team:t1"}
    # The engine ChannelSignal: the channel, its availability and the observation time.
    assert typed["channel_signal"] == {
        "type": "channel_signal",
        "value": {"channel_id": "a2a", "available": False, "observed_at_ms": 5},
    }
    options = request["candidates"]["options"]
    assert [o["option_id"] for o in options] == ["primary_channel", "team_coordination"]

    # A new coordinator (a restart) sees the status only in the engine graph; the
    # status is the engine's snake_case TeamTaskStatus.
    assert graph.nodes["team:t1:task:k1"]["status"] == "working"
    graph.nodes["team:t1:task:k1"]["status"] = "submitted"
    kept = await _coordinator(graph).coordinate(channel_signal="available", **args)
    assert kept["path"] == "primary_channel"
    assert graph.nodes["team:t1:task:k1"]["status"] == "submitted"


@pytest.mark.spec("ARBORA-OPS-R877")
async def test_an_abstention_keeps_the_normal_path_and_names_the_code() -> None:
    graph = _Graph()
    graph.abstain = True
    decided = await _coordinator(graph).coordinate(
        team_id="t2",
        members=["a"],
        tasks={"k": "submitted"},
        channel_signal="available",
        observed_at_ms=1,
    )
    assert decided == {
        "path": "primary_channel",
        "option": "primary_channel",
        "abstained": "TEAM_SIGNAL_STALE",
    }
    assert graph.nodes["team:t2:task:k"]["status"] == "submitted"


@pytest.mark.spec("ARBORA-OPS-R877")
async def test_a_task_status_outside_the_engine_machine_is_refused() -> None:
    graph = _Graph()
    for status in ("assigned", "Submitted", "InputRequired"):
        with pytest.raises(ValueError, match="TeamTaskStatus"):
            await _coordinator(graph).coordinate(
                team_id="t3",
                members=["a"],
                tasks={"k": status},
                channel_signal="available",
                observed_at_ms=1,
            )
    assert graph.nodes == {} and graph.requests == []


def _assert_signals(requests: list[dict[str, Any]]) -> None:
    signals = [_signal(r) for r in requests]
    assert [x["available"] for x in signals] == [False, True]
    assert {x["observed_at_ms"] for x in signals} == {7}


def _bind(authority: Any, graph: _Graph) -> bool:
    return bind_team_coordination(
        authority,
        graph.send,
        graph="g",
        feature_schema=lambda tenant: _PIN,
        principal=lambda: PRINCIPAL,
    )


def _message(signal: str, message_id: str) -> A2AMessage:
    return A2AMessage(
        role="user",
        parts=[A2ATextPart(text="do three steps")],
        message_id=message_id,
        metadata={"channel_signal": signal, "observed_at_ms": 7},
    )


def _dispatch(host: Any, signal: str, key: str) -> Any:
    return host.authority.dispatch(
        message=_message(signal, key),
        idempotency_key=key,
        decision=A2ARouteDecision(agent_name="helper", selection_mode="named"),
    )


@pytest.mark.spec("ARBORA-OPS-R877")
def test_a2a_dispatch_runs_the_team_coordinator_and_status_survives_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, store, checkpoints, clock = _world(monkeypatch)
    declare_question_kinds(monkeypatch, "team_coordination")
    graph = _Graph()
    who = session(frozenset({"kg:read", "kg:write"}))

    async def scenario() -> tuple[str, str]:
        with use_session(who):
            first = Host(engine, store, checkpoints, None, clock)
            assert _bind(first.authority, graph)
            started = await _dispatch(first, "unavailable", "k-down")
            # A restart: a new host over the same engine and graph.
            second = Host(engine, store, checkpoints, None, clock)
            assert _bind(second.authority, graph)
            kept = await _dispatch(second, "available", "k-up")
            return started.id, kept.id

    down_id, up_id = asyncio.run(scenario())

    _assert_signals(graph.requests)
    by_task = {
        task_id: row["metadata"]["a2a_team"]
        for task_id, row in (
            (row["work_item_id"].removeprefix("workitem:orchestrator:"), row)
            for row in engine.work_items.rows.values()
        )
    }
    assert by_task[down_id]["path"] == "team_coordination"
    assert by_task[up_id]["path"] == "primary_channel"
    labels = {node["label"] for node in graph.nodes.values()}
    assert labels == {"Team", "TeamMember", "TeamTask"}
    # Task status lives only in the engine graph: it is there after the restart.
    assert graph.nodes[f"team:{down_id}:task:{down_id}"]["status"] == "working"
    assert graph.nodes[f"team:{up_id}:task:{up_id}"]["status"] == "submitted"


@pytest.mark.spec("ARBORA-OPS-R877")
def test_host_binds_no_team_hook_until_the_contract_names_the_question() -> None:
    class Authority:
        team = None

    authority = Authority()
    assert not _bind(authority, _Graph())
    assert authority.team is None
