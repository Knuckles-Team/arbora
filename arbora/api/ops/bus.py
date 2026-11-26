"""Agent bus operations over the engine's roster, topic and work-item methods (ARBORA-OPS-R882).

``bus.register``, ``bus.heartbeat`` and ``bus.roster`` call the engine bus roster methods.
``bus.send`` and ``bus.receive`` append to and read one per-recipient topic. ``bus.dispatch``
submits one work item for a recipient through the composed ``work_submitter`` port. Arbora
keeps no roster and no mailbox: every effect is an engine call under the verified caller.

A recipient must be on the caller's own tenant roster before any send or dispatch. A
recipient on another tenant answers exactly like an absent one (``BUS_RECIPIENT_UNKNOWN``),
so the operation is no oracle for another tenant's agents. The topic methods are engine
``main`` methods the pinned release lacks, so send and receive refuse ``UNAVAILABLE``
naming the method until the re-pin. The clock is the composed ``clock`` service.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

import msgpack
from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, bound_service, build_read_op, build_write_op
from arbora.api.ops._engine import engine_send
from arbora.api.registry import OpSpec, Verb

READ_SCOPE = "bus:read"
WRITE_SCOPE = "bus:write"
_REF = Field(min_length=1, max_length=256)


@runtime_checkable
class WorkSubmitter(Protocol):
    """The composed port that submits one work item for the caller."""

    async def submit(
        self, *, tenant: str, principal: str, recipient: str, objective: str
    ) -> Mapping[str, Any]: ...


class BusRegisterParams(Params):
    session_ref: str = _REF
    host_ref: str = _REF
    capabilities: list[str] = Field(default_factory=list, max_length=64)


class BusHeartbeatParams(Params):
    session_ref: str = _REF


class BusRosterParams(Params):
    """The roster is the caller's own tenant's; there is nothing to choose."""


class BusSendParams(Params):
    recipient: str = _REF
    body: str = Field(min_length=1, max_length=65536)


class BusReceiveParams(Params):
    session_ref: str = _REF
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=50, ge=1, le=200)


class BusDispatchParams(Params):
    recipient: str = _REF
    objective: str = Field(min_length=1, max_length=8192)


class BusResult(Params):
    value: dict[str, Any]


def _now_ms(context: Any) -> int:
    clock = context.services.get("clock")
    return int(clock()) if clock is not None else time.time_ns() // 1_000_000


def _topic(tenant: str, recipient: str) -> str:
    return f"bus/{tenant}/{recipient}"


async def _roster(context: Any) -> list[dict[str, Any]]:
    answer = await engine_send(
        context,
        "BusRoster",
        {"now_ms": _now_ms(context), "tenant": str(context.caller.tenant)},
    )
    rows = answer.get("participants", []) if isinstance(answer, Mapping) else []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


async def _require_recipient(context: Any, recipient: str) -> None:
    """The recipient is on the caller's tenant roster, or it answers as absent."""
    tenant = str(context.caller.tenant)
    for row in await _roster(context):
        if row.get("session_ref") == recipient and row.get("tenant") == tenant:
            return
    raise OperationRefused("BUS_RECIPIENT_UNKNOWN")


async def handle_bus_register(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    participant = {
        "session_ref": params["session_ref"],
        "host_ref": params["host_ref"],
        "capabilities": list(params.get("capabilities") or ()),
        "tenant": str(context.caller.tenant),
        "last_seen_ms": _now_ms(context),
    }
    answer = await engine_send(
        context, "BusRegister", {"participant": participant}, idempotent=True
    )
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


async def handle_bus_heartbeat(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    answer = await engine_send(
        context,
        "BusHeartbeat",
        {"session_ref": params["session_ref"], "now_ms": _now_ms(context)},
        idempotent=True,
    )
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


async def handle_bus_roster(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    tenant = str(context.caller.tenant)
    rows = [row for row in await _roster(context) if row.get("tenant") == tenant]
    return {"value": {"participants": rows}}


async def handle_bus_send(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Publish to the recipient's stream; the engine's offset log is the topic."""
    await _require_recipient(context, params["recipient"])
    stream = _topic(str(context.caller.tenant), params["recipient"])
    await engine_send(context, "StreamDeclare", {"stream": stream}, idempotent=True)
    payload = msgpack.packb(
        {"sender": str(context.caller.principal), "body": params["body"]}
    )
    answer = await engine_send(
        context,
        "StreamPublish",
        {"stream": stream, "payload": payload, "now_ms": _now_ms(context)},
        idempotent=True,
    )
    return {"value": {"offset": answer}}


async def handle_bus_receive(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Read the session's stream from the caller's offset (``cursor``)."""
    answer = await engine_send(
        context,
        "StreamRead",
        {
            "stream": _topic(str(context.caller.tenant), params["session_ref"]),
            "from_offset": int(params.get("cursor") or 0),
            "max": int(params.get("limit", 50)),
        },
    )
    return {"value": {"messages": list(answer) if isinstance(answer, list) else []}}


async def handle_bus_dispatch(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    submitter: WorkSubmitter = bound_service(
        context, "work_submitter", reason="work submitter is not composed"
    )
    await _require_recipient(context, params["recipient"])
    item = await submitter.submit(
        tenant=str(context.caller.tenant),
        principal=str(context.caller.principal),
        recipient=params["recipient"],
        objective=params["objective"],
    )
    return {"value": dict(item)}


#: Writes replay under the engine method that holds the record, never here.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "bus.register": "medulla:BusRegister",
    "bus.heartbeat": "medulla:BusHeartbeat",
    "bus.send": "medulla:StreamPublish",
    "bus.dispatch": "medulla:SubmitWorkItem",
}


def operations() -> tuple[OpSpec, ...]:
    def write(op_id: str, summary: str, example: str, params: type[Params]) -> OpSpec:
        return build_write_op(
            verb=Verb.ACT,
            op_id=op_id,
            summary=summary,
            examples=(example,),
            params=params,
            result=BusResult,
            handler=f"{__name__}.handle_{op_id.replace('.', '_')}",
            scope=WRITE_SCOPE,
        )

    def read(op_id: str, summary: str, example: str, params: type[Params]) -> OpSpec:
        return build_read_op(
            op_id=op_id,
            summary=summary,
            examples=(example,),
            params=params,
            result=BusResult,
            handler=f"{__name__}.handle_{op_id.replace('.', '_')}",
            scope=READ_SCOPE,
        )

    return (
        write(
            "bus.register",
            "Register an agent session on the bus roster",
            "register this agent on the bus",
            BusRegisterParams,
        ),
        write(
            "bus.heartbeat",
            "Refresh an agent session's last-seen time",
            "keep this agent registered",
            BusHeartbeatParams,
        ),
        read(
            "bus.roster",
            "List the agents on the caller's bus roster",
            "which agents are on the bus",
            BusRosterParams,
        ),
        write(
            "bus.send",
            "Send a message to an agent on the roster",
            "message that agent",
            BusSendParams,
        ),
        read(
            "bus.receive",
            "Read the messages waiting for an agent session",
            "read this agent's messages",
            BusReceiveParams,
        ),
        write(
            "bus.dispatch",
            "Dispatch an objective to an agent as a work item",
            "give that agent this task",
            BusDispatchParams,
        ),
    )


specs = operations
