"""Agent goal operations, served refusing UNAVAILABLE (ARBORA-OPS-R912).

A goal is a run of the ``ARBORA-OPS-R042`` family. The engine has no goal record
or goal method yet, so each operation refuses ``UNAVAILABLE`` rather than keep
goal state in arbora. The shape the engine must serve is in the gaps list.
"""

from __future__ import annotations

from pydantic import Field

from arbora.api.ops._common import Params
from arbora.api.ops._pending import refuse_unavailable, unserved_read, unserved_write
from arbora.api.registry import OpSpec

handle_unserved = refuse_unavailable
READ_SCOPE = "agent:library-read"
CONTROL_SCOPE = "runs:control"


class GoalParams(Params):
    goal_id: str = Field(min_length=1, max_length=256)


class GoalListParams(Params):
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


def operations() -> tuple[OpSpec, ...]:
    m = __name__
    return (
        unserved_write(
            module=m,
            op_id="agents.goals.run",
            summary="Start the run of one goal",
            example="run this goal",
            params=GoalParams,
            scope=CONTROL_SCOPE,
        ),
        unserved_read(
            module=m,
            op_id="agents.goals.list",
            summary="List the goals the caller may see",
            example="which goals exist",
            params=GoalListParams,
            scope=READ_SCOPE,
        ),
        unserved_read(
            module=m,
            op_id="agents.goals.iterations",
            summary="List the iterations of one goal",
            example="show the iterations of this goal",
            params=GoalParams,
            scope=READ_SCOPE,
        ),
        unserved_write(
            module=m,
            op_id="agents.goals.cancel",
            summary="Cancel the run of one goal",
            example="cancel this goal",
            params=GoalParams,
            scope=CONTROL_SCOPE,
        ),
    )


specs = operations

__all__ = ["handle_unserved", "operations", "specs"]
