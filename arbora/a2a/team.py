"""Team coordination over the engine graph and ``Decide`` (ARBORA-OPS-R877).

``TeamCoordinator.coordinate`` writes the ``:Team``, ``:TeamMember`` and ``:TeamTask``
nodes (each task with its status) through the engine graph write path, asks ``Decide``
with the ``TeamCoordination`` question and the observed channel signal with its time,
and applies the returned option: ``primary_channel`` keeps the normal path and
``team_coordination`` starts the team path. Arbora holds no team state: task status
lives only in the engine, so it survives a restart.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import msgpack

from arbora.api.ops._decide import decide_with
from arbora.api.ops._engine import engine_send

PRIMARY = "primary_channel"
TEAM = "team_coordination"

#: ``send(method, params)`` to the decoded engine answer.
TeamSend = Callable[[str, dict[str, Any]], Awaitable[Any]]


def _node(label: str, node_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "properties_msgpack": msgpack.packb({"label": label, "id": node_id, **fields}),
    }


class TeamCoordinator:
    def __init__(self, send: TeamSend, tenant: str) -> None:
        self._send = send
        self._tenant = tenant

    @classmethod
    def over_context(cls, context: Any) -> TeamCoordinator:
        """A coordinator that sends as the verified caller of one operation context."""

        async def send(method: str, params: dict[str, Any]) -> Any:
            return await engine_send(context, method, params, idempotent=True)

        return cls(send, str(context.caller.tenant))

    @classmethod
    def over_host(
        cls, send: Callable[[str, dict[str, Any], str], Awaitable[Any]], tenant: str
    ) -> TeamCoordinator:
        """A coordinator over the host service engine send (the A2A authority's path)."""

        async def bound(method: str, params: dict[str, Any]) -> Any:
            return await send(method, params, tenant)

        return cls(bound, tenant)

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
        send = self._send
        await send("CreateNodeIfAbsent", _node("Team", f"team:{team_id}"))
        for member in members:
            await send(
                "CreateNodeIfAbsent",
                _node("TeamMember", f"team:{team_id}:member:{member}", team=team_id),
            )
        for task_id, status in tasks.items():
            await send(
                "CreateNodeIfAbsent",
                _node("TeamTask", f"team:{team_id}:task:{task_id}", status=status),
            )
        record = await decide_with(
            send,
            self._tenant,
            "team_coordination",
            {
                "team_id": team_id,
                "channel_signal": channel_signal,
                "observed_at_ms": observed_at_ms,
            },
        )
        option = str(record.get("option") or PRIMARY)
        if option == TEAM:
            for task_id in tasks:
                await send(
                    "CompareAndSetNodeFields",
                    {
                        "node_id": f"team:{team_id}:task:{task_id}",
                        "conditions_msgpack": msgpack.packb({}),
                        "updates_msgpack": msgpack.packb({"status": "assigned"}),
                    },
                )
        return {"path": TEAM if option == TEAM else PRIMARY, "option": option}


#: ``hook(session, message, task_id, owner_ref, agent_name, now_ms)`` is the one call
#: ``WorkItemA2AAuthority.dispatch`` makes; it returns the team record for the task metadata.
TeamHook = Callable[..., Awaitable[dict[str, Any]]]

#: The two channel signals a caller may report on the message metadata.
AVAILABLE = "available"
UNAVAILABLE = "unavailable"


def host_team_hook(
    send: Callable[[str, dict[str, Any], str], Awaitable[Any]],
) -> TeamHook:
    """The dispatch hook over the host service engine send (ARBORA-OPS-R877).

    The signal is what the caller observed about its channel and when
    (``channel_signal`` and ``observed_at_ms`` on the message metadata); a message that
    reports none observed an available channel at the admission time.
    """

    async def hook(
        session: Any,
        message: Any,
        task_id: str,
        owner_ref: str,
        agent_name: str,
        now_ms: int,
    ) -> dict[str, Any]:
        reported = message.metadata.get("channel_signal")
        signal = UNAVAILABLE if reported == UNAVAILABLE else AVAILABLE
        observed = message.metadata.get("observed_at_ms")
        decided = await TeamCoordinator.over_host(send, str(session.tenant)).coordinate(
            team_id=task_id,
            members=(owner_ref, agent_name),
            tasks={task_id: "submitted"},
            channel_signal=signal,
            observed_at_ms=observed if type(observed) is int else now_ms,
        )
        return {"team_id": task_id, "path": decided["path"]}

    return hook


def bind_team_coordination(
    authority: Any, send: Callable[[str, dict[str, Any], str], Awaitable[Any]] | None
) -> bool:
    """Hook the coordinator into ``authority.dispatch`` once the contract names the question.

    Returns whether the hook was bound (ARBORA-OPS-R877). A host with no engine send, or a
    pinned engine that lacks the ``team_coordination`` question, keeps the normal path only.
    """
    from arbora.pin_lag.medulla import question_kind_served

    if send is None or not hasattr(authority, "team"):
        return False
    if not question_kind_served("team_coordination"):
        return False
    authority.team = host_team_hook(send)
    return True


__all__ = [
    "AVAILABLE",
    "PRIMARY",
    "TEAM",
    "UNAVAILABLE",
    "TeamCoordinator",
    "TeamHook",
    "TeamSend",
    "bind_team_coordination",
    "host_team_hook",
]
