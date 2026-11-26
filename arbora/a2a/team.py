"""Team coordination over the engine graph and ``Decide`` (ARBORA-OPS-R877).

``TeamCoordinator.coordinate`` writes the ``:Team``, ``:TeamMember`` and ``:TeamTask``
nodes (each task with its status) through the engine graph write path, asks ``Decide``
with the ``TeamCoordination`` question and the observed channel signal with its time,
and applies the returned option: ``primary_channel`` keeps the normal path and
``team_coordination`` starts the team path. Arbora holds no team state: task status
lives only in the engine, so it survives a restart.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import msgpack

from arbora.api.ops._decide import decide_record
from arbora.api.ops._engine import engine_send

PRIMARY = "primary_channel"
TEAM = "team_coordination"


def _node(label: str, node_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "properties_msgpack": msgpack.packb({"label": label, "id": node_id, **fields}),
    }


class TeamCoordinator:
    def __init__(self, context: Any) -> None:
        self._context = context

    async def coordinate(
        self,
        *,
        team_id: str,
        members: Sequence[str],
        tasks: Mapping[str, str],
        channel_signal: str,
        observed_at_ms: int,
    ) -> dict[str, Any]:
        """Write the team records, decide, and apply the option."""
        send = self._context
        await engine_send(
            send,
            "CreateNodeIfAbsent",
            _node("Team", f"team:{team_id}"),
            idempotent=True,
        )
        for member in members:
            await engine_send(
                send,
                "CreateNodeIfAbsent",
                _node("TeamMember", f"team:{team_id}:member:{member}", team=team_id),
                idempotent=True,
            )
        for task_id, status in tasks.items():
            await engine_send(
                send,
                "CreateNodeIfAbsent",
                _node("TeamTask", f"team:{team_id}:task:{task_id}", status=status),
                idempotent=True,
            )
        record = await decide_record(
            send,
            "TeamCoordination",
            {
                "team_id": team_id,
                "channel_signal": channel_signal,
                "observed_at_ms": observed_at_ms,
            },
        )
        option = str(record.get("option") or PRIMARY)
        if option == TEAM:
            for task_id in tasks:
                await engine_send(
                    send,
                    "CompareAndSetNodeFields",
                    {
                        "node_id": f"team:{team_id}:task:{task_id}",
                        "conditions_msgpack": msgpack.packb({}),
                        "updates_msgpack": msgpack.packb({"status": "assigned"}),
                    },
                    idempotent=True,
                )
        return {"path": TEAM if option == TEAM else PRIMARY, "option": option}


__all__ = ["PRIMARY", "TEAM", "TeamCoordinator"]
