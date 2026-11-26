"""Team coordination writes records and applies the decision (ARBORA-OPS-R877)."""

from __future__ import annotations

import msgpack
import pytest

from arbora.a2a import team
from arbora.a2a.team import TeamCoordinator
from arbora.api.ops import _decide
from tests.api._engine_double import EngineDouble
from tests.api._engine_double import context as engine_context


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
    started = await TeamCoordinator(engine_context()).coordinate(
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
    kept = await TeamCoordinator(engine_context()).coordinate(
        channel_signal="available", **args
    )
    assert kept["path"] == "primary_channel"
    assert graph["team:t1:task:k1"]["status"] == "assigned"
