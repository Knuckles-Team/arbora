"""Agent goal operations over the engine goal methods (ARBORA-OPS-R912).

A goal is a run of the ``ARBORA-OPS-R042`` family, and the engine holds its
record and its state machine: ``GoalList``, ``GoalIterations`` and
``GoalControl``. Arbora keeps no goal state and reaches the methods behind the
medulla pin-lag guard, so a pinned engine without one refuses ``UNAVAILABLE``
naming it. An absent, foreign or malformed goal answers the engine's own
``GOAL_NOT_FOUND``, and a transition the machine refuses keeps its code.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.engine_calls import engine_send
from arbora.api.ops.knowledge import EngineReply
from arbora.api.registry import OpSpec, Verb
from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_methods

READ_SCOPE = "agent:library-read"
CONTROL_SCOPE = "runs:control"
#: Control writes replay under the engine goal machine, which keys on the goal.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "agents.goals.run": "medulla:GoalControl",
    "agents.goals.cancel": "medulla:GoalControl",
}


class GoalParams(Params):
    goal_id: str = Field(min_length=1, max_length=256)


class GoalListParams(Params):
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


async def _send(
    context: Any, method: str, params: dict[str, Any], *, write: bool = False
) -> dict[str, Any]:
    try:
        require_methods(method)
    except PinLagUnavailable as error:
        raise refusal(error) from error
    answer = await engine_send(context, method, params, idempotent=write)
    return dict(answer) if isinstance(answer, Mapping) else {}


async def _control(context: Any, operation: str, goal_id: str) -> dict[str, Any]:
    answer = await _send(
        context,
        "GoalControl",
        {"op": {"operation": operation, "goal_id": goal_id}},
        write=True,
    )
    return {"value": answer}


async def handle_goals_run(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _control(context, "run", params["goal_id"])


async def handle_goals_cancel(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _control(context, "cancel", params["goal_id"])


async def handle_goals_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request: dict[str, Any] = {"limit": params["limit"]}
    if params.get("cursor"):
        request["cursor"] = params["cursor"]
    return {"value": await _send(context, "GoalList", request)}


async def handle_goals_iterations(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return {
        "value": await _send(context, "GoalIterations", {"goal_id": params["goal_id"]})
    }


def operations() -> tuple[OpSpec, ...]:
    def read(
        op_id: str, summary: str, example: str, params: type[Params], handler: str
    ) -> OpSpec:
        return build_read_op(
            op_id=op_id,
            summary=summary,
            examples=(example,),
            params=params,
            result=EngineReply,
            handler=f"{__name__}.{handler}",
            scope=READ_SCOPE,
        )

    def write(op_id: str, summary: str, example: str, handler: str) -> OpSpec:
        return build_write_op(
            verb=Verb.ACT,
            op_id=op_id,
            summary=summary,
            examples=(example,),
            params=GoalParams,
            result=EngineReply,
            handler=f"{__name__}.{handler}",
            scope=CONTROL_SCOPE,
        )

    return (
        write(
            "agents.goals.run",
            "Start the run of one goal",
            "run this goal",
            "handle_goals_run",
        ),
        read(
            "agents.goals.list",
            "List the goals the caller may see",
            "which goals exist",
            GoalListParams,
            "handle_goals_list",
        ),
        read(
            "agents.goals.iterations",
            "List the iterations of one goal",
            "show the iterations of this goal",
            GoalParams,
            "handle_goals_iterations",
        ),
        write(
            "agents.goals.cancel",
            "Cancel the run of one goal",
            "cancel this goal",
            "handle_goals_cancel",
        ),
    )


specs = operations

__all__ = ["NATIVE_IDEMPOTENCY", "operations", "specs"]
