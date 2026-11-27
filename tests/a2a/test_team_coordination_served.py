"""Team coordination writes records and applies the decision (ARBORA-OPS-R877)."""

from __future__ import annotations

import asyncio
from typing import Any

import msgpack
import pytest

from arbora.a2a import team
from arbora.a2a.models import A2AMessage, A2ARouteDecision, A2ATextPart
from arbora.a2a.team import TeamCoordinator, bind_team_coordination
from arbora.api.ops import _decide
from arbora.security.session import use_session
from tests.a2a.test_checkpoint_resume_served import Host, _world, lenient_decode
from tests.a2a.test_operation_bridge import session
from tests.api._engine_double import EngineDouble
from tests.api._engine_double import context as engine_context
from tests.question_kinds import declare_question_kinds

__all__ = ["lenient_decode"]


@pytest.mark.spec("ARBORA-OPS-R877")
async def test_unavailable_starts_team_available_keeps_primary_and_status_survives_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph: dict[str, dict] = {}
    answer = {"option": "team_coordination"}
    engine = EngineDouble(monkeypatch)

    def create(p):
        graph.setdefault(
            p["node_id"], msgpack.unpackb(p["properties_msgpack"], raw=False)
        )
        return {}

    def update(p):
        graph[p["node_id"]].update(msgpack.unpackb(p["updates_msgpack"], raw=False))
        return {}

    engine.serve("CreateNodeIfAbsent", create)
    engine.serve("CompareAndSetNodeFields", update)
    engine.serve("Decide", lambda p: answer)
    engine.install(team)
    engine.install(_decide)
    args = {
        "team_id": "t1",
        "members": ["a", "b"],
        "tasks": {"k1": "open"},
        "observed_at_ms": 5,
    }
    started = await TeamCoordinator.over_context(engine_context()).coordinate(
        channel_signal="unavailable", **args
    )
    assert started["path"] == "team_coordination"
    labels = {n["label"] for n in graph.values()}
    assert labels == {"Team", "TeamMember", "TeamTask"}
    question = [p for m, p, _ in engine.sent if m == "Decide"][0]["request"]["question"]
    assert (
        question["channel_signal"] == "unavailable" and question["observed_at_ms"] == 5
    )

    # A new coordinator (a restart) sees the status only in the engine graph.
    assert graph["team:t1:task:k1"]["status"] == "assigned"
    answer["option"] = "primary_channel"
    kept = await TeamCoordinator.over_context(engine_context()).coordinate(
        channel_signal="available", **args
    )
    assert kept["path"] == "primary_channel"
    assert graph["team:t1:task:k1"]["status"] == "assigned"


class _Graph:
    """The engine graph and decision behind the host service send."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.questions: list[dict[str, Any]] = []

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
        question = params["request"]["question"]
        self.questions.append(question)
        unavailable = question["channel_signal"] == "unavailable"
        return {"option": "team_coordination" if unavailable else "primary_channel"}


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
            assert bind_team_coordination(first.authority, graph.send)
            started = await _dispatch(first, "unavailable", "k-down")
            # A restart: a new host over the same engine and graph.
            second = Host(engine, store, checkpoints, None, clock)
            assert bind_team_coordination(second.authority, graph.send)
            kept = await _dispatch(second, "available", "k-up")
            return started.id, kept.id

    down_id, up_id = asyncio.run(scenario())

    assert [q["channel_signal"] for q in graph.questions] == [
        "unavailable",
        "available",
    ]
    assert all(q["observed_at_ms"] == 7 for q in graph.questions)
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
    assert graph.nodes[f"team:{down_id}:task:{down_id}"]["status"] == "assigned"
    assert graph.nodes[f"team:{up_id}:task:{up_id}"]["status"] == "submitted"


@pytest.mark.spec("ARBORA-OPS-R877")
def test_host_binds_no_team_hook_until_the_contract_names_the_question() -> None:
    class Authority:
        team = None

    authority = Authority()
    assert not bind_team_coordination(authority, _Graph().send)
    assert authority.team is None
