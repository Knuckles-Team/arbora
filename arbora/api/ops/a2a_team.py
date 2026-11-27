"""The team projection served as an operation (ARBORA-HOST-R026.6).

``a2a.team.get`` reads one team, its bound members and each task status from the engine
graph (the ``Team``, ``TeamMember`` and ``TeamTask`` nodes of the A2A ontology), and adds the
state of the A2A task WorkItem each team task names when the A2A service is composed. Arbora
adds no store: a fresh host answers the same record. A team the caller cannot see answers
exactly like an absent one, ``NOT_FOUND``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.a2a.team import read_team
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.ops.engine_calls import engine_send
from arbora.api.registry import OpSpec

READ_SCOPE = "kg:read"


class TeamGetParams(_common.Params):
    team_id: str = Field(min_length=1, max_length=256)


class TeamGetResult(_common.Params):
    value: dict[str, Any]


async def _a2a_state(context: Any, task_id: str) -> str | None:
    """The state of the task's A2A WorkItem, or ``None`` when it is not composed or not visible."""
    service = context.services.get("a2a")
    if service is None or context.caller.session is None:
        return None
    from arbora.security.session import use_session

    with use_session(context.caller.session):
        task = await service.get_task(task_id)
    return None if task is None else str(task.status.state)


async def handle_team_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    async def send(method: str, request: dict[str, Any]) -> Any:
        return await engine_send(context, method, request)

    team = await read_team(send, str(params["team_id"]))
    if team is None:
        raise OperationRefused("NOT_FOUND")
    for task in team["tasks"]:
        task["a2a_state"] = await _a2a_state(context, task["task_id"])
    return {"value": team}


def operations() -> tuple[OpSpec, ...]:
    return (
        _common.build_read_op(
            op_id="a2a.team.get",
            summary="Read a team, its members and its task statuses",
            examples=("show the team working this task",),
            params=TeamGetParams,
            result=TeamGetResult,
            handler="arbora.api.ops.a2a_team.handle_team_get",
            scope=READ_SCOPE,
        ),
    )


specs = operations

__all__ = ["TeamGetParams", "TeamGetResult", "handle_team_get", "operations"]
