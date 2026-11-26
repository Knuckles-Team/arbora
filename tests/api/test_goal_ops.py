"""Goal operations over the engine goal machine (ARBORA-OPS-R912)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import goals
from arbora.api.registry.pending_scopes import load_pending_engine_scopes
from tests.api._engine_double import EngineDouble, context

pytestmark = pytest.mark.spec("ARBORA-OPS-R912")
_NO_OP: Any = None
_BY_ID = {op.id: op for op in goals.operations()}


def test_goal_scopes_follow_the_engine_catalog() -> None:
    assert set(_BY_ID) == {
        "agents.goals.run",
        "agents.goals.list",
        "agents.goals.iterations",
        "agents.goals.cancel",
    }
    assert _BY_ID["agents.goals.run"].scopes == frozenset({"runs:control"})
    assert "runs:control" in load_pending_engine_scopes()
    assert _BY_ID["agents.goals.list"].scopes == frozenset({"agent:library-read"})
    assert set(goals.NATIVE_IDEMPOTENCY) == {"agents.goals.run", "agents.goals.cancel"}


@pytest.mark.parametrize(
    ("handler", "params", "method"),
    [
        ("handle_goals_run", {"goal_id": "g"}, "GoalControl"),
        ("handle_goals_cancel", {"goal_id": "g"}, "GoalControl"),
        ("handle_goals_list", {"limit": 5}, "GoalList"),
        ("handle_goals_iterations", {"goal_id": "g"}, "GoalIterations"),
    ],
)
async def test_a_pinned_engine_without_the_method_refuses_naming_it(
    handler: str, params: dict[str, Any], method: str
) -> None:
    with pytest.raises(OperationRefused) as refused:
        await getattr(goals, handler)(context(), params, _NO_OP)
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == method


async def test_cancel_goal_uses_the_run_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, str] = {"g1": "running", "g2": "succeeded"}
    engine = EngineDouble(monkeypatch)

    def control(params: dict[str, Any]) -> Any:
        op = params["op"]
        goal = op["goal_id"]
        if goal not in state:
            raise EngineRefusal("GOAL_NOT_FOUND")
        if op["operation"] == "cancel":
            if state[goal] != "running":
                raise EngineRefusal("TRANSITION_REFUSED")
            state[goal] = "cancelled"
        else:
            if state[goal] == "running":
                raise EngineRefusal("TRANSITION_REFUSED")
            state[goal] = "running"
        return {"goal_id": goal, "state": state[goal], "replayed": False}

    engine.serve("GoalControl", control)
    engine.serve(
        "GoalList",
        lambda p: {"goals": [{"goal_id": g} for g in state], "next_cursor": None},
    )
    engine.serve("GoalIterations", lambda p: {"iterations": [{"iteration": 1}]})
    engine.install(goals)
    cancelled = await goals.handle_goals_cancel(context(), {"goal_id": "g1"}, _NO_OP)
    assert cancelled["value"]["state"] == "cancelled"
    assert engine.sent[0] == (
        "GoalControl",
        {"op": {"operation": "cancel", "goal_id": "g1"}},
        True,
    )
    # The machine, not arbora, refuses a second cancel and an unknown goal.
    with pytest.raises(EngineRefusal) as again:
        await goals.handle_goals_cancel(context(), {"goal_id": "g1"}, _NO_OP)
    assert again.value.code == "TRANSITION_REFUSED"
    with pytest.raises(EngineRefusal) as absent:
        await goals.handle_goals_cancel(context(), {"goal_id": "nope"}, _NO_OP)
    assert absent.value.code == "GOAL_NOT_FOUND"
    rerun = await goals.handle_goals_run(context(), {"goal_id": "g2"}, _NO_OP)
    assert rerun["value"]["state"] == "running"
    listed = await goals.handle_goals_list(
        context(), {"limit": 5, "cursor": None}, _NO_OP
    )
    assert [g["goal_id"] for g in listed["value"]["goals"]] == ["g1", "g2"]
    its = await goals.handle_goals_iterations(context(), {"goal_id": "g1"}, _NO_OP)
    assert its["value"]["iterations"] == [{"iteration": 1}]
