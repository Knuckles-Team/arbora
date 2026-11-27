"""Team coordination over the engine graph and ``Decide`` (ARBORA-OPS-R877).

``TeamCoordinator.coordinate`` writes the ``Team``, ``TeamMember`` and ``TeamTask``
nodes (each task with its ``TeamTaskStatus`` in the engine's snake_case wire form)
through the engine graph write path, asks ``Decide`` with the ``team_coordination``
question and the typed ``ChannelSignal`` (channel, availability, observation time), and
applies the returned option: ``primary_channel`` keeps the normal path and
``team_coordination`` starts the team path. The engine reads the team and its bound
members from the graph (``_team``, ``_team_member`` and ``boundToTeam``); an abstention
(a stale signal, no bound member) keeps the normal path and is named on the record.
Arbora holds no team state: task status lives only in the engine, so it survives a
restart.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import msgpack

PRIMARY = "primary_channel"
TEAM = "team_coordination"

#: The engine ``TeamTaskStatus`` wire values (snake_case).
TASK_STATUSES = frozenset(
    {"submitted", "working", "input_required", "completed", "canceled", "failed"}
)
#: The status a task takes when the team path accepts it (the machine's ``Accept``).
ACCEPTED = "working"

#: ``send(method, params)`` to the decoded engine answer.
TeamSend = Callable[[str, dict[str, Any]], Awaitable[Any]]


def _node(label: str, node_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "properties_msgpack": msgpack.packb({"label": label, "id": node_id, **fields}),
    }


def team_request(
    tenant: str,
    feature_schema: Mapping[str, Any],
    *,
    graph: str,
    team_node: str,
    channel_id: str,
    available: bool,
    observed_at_ms: int,
) -> dict[str, Any]:
    """The engine ``DecideRequest`` of one ``team_coordination`` question."""
    from medulla.decision_client import DeclaredOption, decide_request, declared, param

    signal = {
        "channel_id": channel_id,
        "available": available,
        "observed_at_ms": observed_at_ms,
    }
    return decide_request(
        tenant,
        (f"team-coordination:{team_node}", "team_coordination", "ordinary"),
        declared([DeclaredOption(PRIMARY), DeclaredOption(TEAM)]),
        feature_schema,
        params=[
            param("graph", "text", graph),
            param("team", "text", team_node),
            param("channel_signal", "channel_signal", signal),
        ],
    )


def chosen_option(record: Mapping[str, Any]) -> tuple[str, str | None]:
    """The option the sealed record concludes and the abstention code, if it abstained.

    An advisory outcome scores the two options and the concluded one scores one. An
    abstention keeps the normal path and carries the engine's reason code.
    """
    outcome = record.get("outcome")
    if not isinstance(outcome, Mapping):
        raise ValueError("the engine answered no team coordination outcome")
    if outcome.get("outcome") == "abstained":
        reasons = outcome.get("reasons") or ()
        code = next(
            (str(r["code"]) for r in reasons if isinstance(r, Mapping) and "code" in r),
            "abstained",
        )
        return PRIMARY, code
    scores = outcome.get("scores")
    if outcome.get("outcome") != "advisory" or not isinstance(scores, Sequence):
        raise ValueError("the engine answered no team coordination outcome")
    best = max(scores, key=lambda row: int(row["score"]["value"]))
    option = str(best["option_id"])
    if option not in (PRIMARY, TEAM):
        raise ValueError("the engine concluded an unknown team option")
    return option, None


class TeamCoordinator:
    def __init__(
        self,
        send: TeamSend,
        tenant: str,
        *,
        graph: str,
        feature_schema: Mapping[str, Any],
        principal: str,
    ) -> None:
        self._send = send
        self._tenant = tenant
        self._graph = graph
        self._feature_schema = feature_schema
        self._principal = principal

    @classmethod
    def over_host(
        cls,
        send: Callable[[str, dict[str, Any], str], Awaitable[Any]],
        tenant: str,
        *,
        graph: str,
        feature_schema: Mapping[str, Any],
        principal: str,
    ) -> TeamCoordinator:
        """A coordinator over the host service engine send (the A2A authority's path)."""

        async def bound(method: str, params: dict[str, Any]) -> Any:
            return await send(method, params, tenant)

        return cls(
            bound,
            tenant,
            graph=graph,
            feature_schema=feature_schema,
            principal=principal,
        )

    async def coordinate(
        self,
        *,
        team_id: str,
        members: Sequence[str],
        tasks: Mapping[str, str],
        channel_signal: str,
        observed_at_ms: int,
        channel_id: str = "a2a",
    ) -> dict[str, Any]:
        """Write the team records, decide, and apply the option."""
        from arbora.deployment.placement import (
            check_receipt,
            send_decide,
            single_record,
        )

        unknown = sorted(set(tasks.values()) - TASK_STATUSES)
        if unknown:
            raise ValueError(f"not a TeamTaskStatus: {', '.join(unknown)}")
        send = self._send
        team_node = f"team:{team_id}"
        await send("CreateNodeIfAbsent", _node("Team", team_node, _team=True))
        for member in members:
            await send(
                "CreateNodeIfAbsent",
                _node(
                    "TeamMember",
                    f"{team_node}:member:{member}",
                    _team_member=True,
                    boundToTeam=team_node,
                ),
            )
        for task_id, status in tasks.items():
            await send(
                "CreateNodeIfAbsent",
                _node("TeamTask", f"{team_node}:task:{task_id}", status=status),
            )
        request = team_request(
            self._tenant,
            self._feature_schema,
            graph=self._graph,
            team_node=team_node,
            channel_id=channel_id,
            available=channel_signal != UNAVAILABLE,
            observed_at_ms=observed_at_ms,
        )
        record = single_record(await send_decide(send, request))
        check_receipt(record, request, self._principal)
        option, abstained = chosen_option(record)
        if option == TEAM:
            for task_id in tasks:
                await send(
                    "CompareAndSetNodeFields",
                    {
                        "node_id": f"{team_node}:task:{task_id}",
                        "conditions_msgpack": msgpack.packb({}),
                        "updates_msgpack": msgpack.packb({"status": ACCEPTED}),
                    },
                )
        applied = {"path": option, "option": option}
        if abstained is not None:
            applied["abstained"] = abstained
        return applied


#: ``hook(session, message, task_id, owner_ref, agent_name, now_ms)`` is the one call
#: ``WorkItemA2AAuthority.dispatch`` makes; it returns the team record for the task metadata.
TeamHook = Callable[..., Awaitable[dict[str, Any]]]

#: The two channel signals a caller may report on the message metadata.
AVAILABLE = "available"
UNAVAILABLE = "unavailable"


def host_team_hook(
    send: Callable[[str, dict[str, Any], str], Awaitable[Any]],
    *,
    graph: str,
    feature_schema: Callable[[str], Mapping[str, Any]],
    principal: Callable[[], str],
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
        tenant = str(session.tenant)
        decided = await TeamCoordinator.over_host(
            send,
            tenant,
            graph=graph,
            feature_schema=feature_schema(tenant),
            principal=principal(),
        ).coordinate(
            team_id=task_id,
            members=(owner_ref, agent_name),
            tasks={task_id: "submitted"},
            channel_signal=signal,
            observed_at_ms=observed if type(observed) is int else now_ms,
        )
        return {"team_id": task_id, **decided}

    return hook


def bind_team_coordination(
    authority: Any,
    send: Any,
    *,
    graph: str = "",
    feature_schema: Any = None,
    principal: Any = None,
) -> bool:
    """Hook the coordinator into ``authority.dispatch`` once the contract names the question.

    Returns whether the hook was bound (ARBORA-OPS-R877). ``send`` is the host service engine
    send, ``feature_schema`` the tenant pin and ``principal`` the service identity; a host that
    lacks any of them, or a
    pinned engine that lacks the ``team_coordination`` question, keeps the normal path only.
    """
    from arbora.pin_lag.medulla import question_kind_served

    if None in (send, feature_schema, principal) or not hasattr(authority, "team"):
        return False
    if not question_kind_served("team_coordination"):
        return False
    authority.team = host_team_hook(
        send, graph=graph, feature_schema=feature_schema, principal=principal
    )
    return True


__all__ = [
    "AVAILABLE",
    "PRIMARY",
    "TEAM",
    "UNAVAILABLE",
    "TASK_STATUSES",
    "TeamCoordinator",
    "TeamHook",
    "TeamSend",
    "bind_team_coordination",
    "chosen_option",
    "host_team_hook",
    "team_request",
]
