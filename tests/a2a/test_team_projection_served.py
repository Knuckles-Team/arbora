"""Teams are graph nodes the host reads back; the A2A fallback is visible there (ARBORA-HOST-R026.6)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import msgpack
import pytest

from arbora.a2a.team import read_team
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import a2a_team
from arbora.security.session import use_session
from tests.a2a.test_checkpoint_resume_served import Host, _world
from tests.a2a.test_operation_bridge import session
from tests.a2a.test_team_coordination_served import (
    _bind,
    _coordinator,
    _dispatch,
    _Graph,
)
from tests.question_kinds import declare_question_kinds


class ReadableGraph(_Graph):
    """The engine graph with the label read the projection uses."""

    async def send(self, method: str, params: dict[str, Any], tenant: str) -> Any:
        if method != "GetNodesByLabel":
            return await super().send(method, params, tenant)
        after = params.get("after", "")
        rows = [
            (node_id, msgpack.packb(props))
            for node_id, props in sorted(self.nodes.items())
            if props["label"] == params["label"] and node_id > after
        ]
        return rows[: params["limit"]]


def _reader(graph: ReadableGraph) -> Any:
    async def send(method: str, params: dict[str, Any]) -> Any:
        return await graph.send(method, params, "tenant:one")

    return send


async def _coordinated(graph: ReadableGraph) -> None:
    await _coordinator(graph).coordinate(
        team_id="t1",
        members=["b", "a"],
        tasks={"k2": "submitted", "k1": "submitted"},
        channel_signal="unavailable",
        observed_at_ms=5,
    )


@pytest.mark.spec("ARBORA-HOST-R026.6")
async def test_a_team_is_discoverable_with_its_members_and_task_states() -> None:
    graph = ReadableGraph()
    await _coordinated(graph)
    task = graph.nodes["team:t1:task:k1"]
    # The ontology terms as they are: the task is bound to its team and carries taskStatus.
    assert task["boundToTeam"] == "team:t1" and task["taskStatus"] == "working"
    team = await read_team(_reader(graph), "t1")
    assert team == {
        "team_id": "t1",
        "members": ["a", "b"],
        "tasks": [
            {"task_id": "k1", "status": "working"},
            {"task_id": "k2", "status": "working"},
        ],
    }
    # Another team's members and tasks are never folded in; an absent team is None.
    await _coordinator(graph).coordinate(
        team_id="t2",
        members=["c"],
        tasks={"x": "submitted"},
        channel_signal="available",
        observed_at_ms=5,
    )
    other = await read_team(_reader(graph), "t2")
    assert other is not None and other["members"] == ["c"]
    assert other["tasks"] == [{"task_id": "x", "status": "submitted"}]
    assert await read_team(_reader(graph), "ghost") is None


def _context(graph: ReadableGraph, service: Any = None) -> Any:
    return SimpleNamespace(
        services={"a2a": service} if service else {},
        caller=SimpleNamespace(session=None),
    )


@pytest.mark.spec("ARBORA-HOST-R026.6")
async def test_the_served_read_refuses_an_unknown_team_and_joins_the_a2a_task_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = ReadableGraph()
    await _coordinated(graph)

    async def engine_send(context: Any, method: str, params: dict[str, Any]) -> Any:
        return await graph.send(method, params, "tenant:one")

    monkeypatch.setattr(a2a_team, "engine_send", engine_send)
    op = a2a_team.operations()[0]
    served = await a2a_team.handle_team_get(_context(graph), {"team_id": "t1"}, op)
    assert [t["task_id"] for t in served["value"]["tasks"]] == ["k1", "k2"]
    assert {t["a2a_state"] for t in served["value"]["tasks"]} == {None}
    with pytest.raises(OperationRefused) as refused:
        await a2a_team.handle_team_get(_context(graph), {"team_id": "ghost"}, op)
    assert refused.value.code == "NOT_FOUND"


@pytest.mark.spec("ARBORA-HOST-R026.6")
def test_a_down_channel_hands_the_step_to_the_team_and_a_fresh_host_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, store, checkpoints, clock = _world(monkeypatch)
    declare_question_kinds(monkeypatch, "team_coordination")
    graph = ReadableGraph()
    who = session(frozenset({"kg:read", "kg:write"}))

    async def scenario() -> tuple[str, dict[str, Any] | None]:
        with use_session(who):
            first = Host(engine, store, checkpoints, None, clock)
            assert _bind(first.authority, graph)
            sent = await _dispatch(first, "unavailable", "k-down")
            # A restart: a new host and a new reader over the same engine graph.
            second = Host(engine, store, checkpoints, None, clock)
            assert _bind(second.authority, graph)
            return sent.id, await read_team(_reader(graph), sent.id)

    task_id, team = asyncio.run(scenario())
    assert team is not None and team["team_id"] == task_id
    # The task went out over the A2A path and its status is visible in the team record.
    assert team["tasks"] == [{"task_id": task_id, "status": "working"}]
    assert len(team["members"]) == 2
