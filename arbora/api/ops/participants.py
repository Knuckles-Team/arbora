"""Participant memory served from the engine (ARBORA-OPS-R897.1 to R897.5).

``participant.*`` forwards the engine ``Participant`` and ``ParticipantRead``
methods and appends a turn through ``MemoryRemember``. Arbora decides nothing
about memory: the engine owns every fact, rule and refusal and answers a
participant the caller may not see exactly like an absent one. Arbora fills
the tenant and the owner from the verified caller, so a body never names them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal

from pydantic import Field

from arbora.api.errors import EngineRefusal
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.engine_calls import engine_send
from arbora.api.ops.knowledge import EngineReply
from arbora.api.registry import OpSpec, Verb

READ_SCOPE = "participant:read"
WRITE_SCOPE = "participant:write"
#: The engine replays each of these writes by its idempotency key.
NATIVE_IDEMPOTENCY: Mapping[str, str] = MappingProxyType(
    {
        "participant.put": "medulla:Participant",
        "participant.member.add": "medulla:Participant",
        "participant.scope.put": "medulla:Participant",
        "participant.observation.set": "medulla:Participant",
        "participant.turn.append": "medulla:MemoryRemember",
    }
)
_SCHEMA_VERSION = 1
_Id = Field(min_length=1, max_length=256)
_Observer = Field(
    default="system",
    min_length=1,
    max_length=256,
    description="'system' or a participant id",
)


class ParticipantPutParams(Params):
    participant_id: str = _Id
    workspace: str = _Id
    kind: Literal["person", "agent", "object"]
    display_name: str = Field(min_length=1, max_length=256)
    represents: str | None = Field(default=None, min_length=1, max_length=256)
    acts_as: list[str] = Field(default_factory=list, max_length=64)


class MemberAddParams(Params):
    session_id: str = _Id
    participant_id: str = _Id


class ParticipantGetParams(Params):
    participant_id: str = _Id


class ParticipantListParams(Params):
    pass


class ScopePutParams(Params):
    scope_id: str = _Id
    name: str = Field(min_length=1, max_length=256)
    session_ids: list[str] = Field(default_factory=list, max_length=256)
    shared_with: list[str] = Field(default_factory=list, max_length=256)


class ObservationSetParams(Params):
    session_id: str = _Id
    participant_id: str = _Id
    observe_self: bool
    observe_others: bool


class TurnAppendParams(Params):
    session_id: str = _Id
    speaker: str = _Id
    text: str = Field(min_length=1, max_length=16384)
    timestamp_ms: int = Field(ge=0)


class ParticipantAskParams(Params):
    subject: str = _Id
    question: str = Field(min_length=1, max_length=2048)
    observer: str = _Observer
    memory_scope: str | None = Field(default=None, min_length=1, max_length=256)
    as_of_ms: int | None = Field(default=None, ge=0)


class ParticipantCardParams(Params):
    subject: str = _Id
    observer: str = _Observer


class TurnResult(Params):
    turn_committed: bool
    memory_id: str
    derive: Literal["enqueued", "deferred"]


def _observer(value: str) -> Any:
    """The engine's observer reference: the word ``system`` or a participant."""
    return "system" if value == "system" else {"participant": value}


async def _participant(context: Any, op: dict[str, Any], *, write: bool) -> Any:
    method = "Participant" if write else "ParticipantRead"
    return await engine_send(context, method, {"op": op}, idempotent=write)


async def handle_participant_put(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Create or replace one participant in the caller's tenant."""
    participant = {
        "participant_id": params["participant_id"],
        "tenant": str(context.caller.tenant),
        "workspace": params["workspace"],
        "kind": params["kind"],
        "display_name": params["display_name"],
        "represents": params.get("represents"),
        "acts_as": list(params.get("acts_as") or ()),
        "schema_version": _SCHEMA_VERSION,
    }
    answer = await _participant(
        context, {"op": "put", "participant": participant}, write=True
    )
    return {"value": answer}


async def handle_member_add(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Add one participant to a session."""
    body = {"op": "add_member", **params}
    return {"value": await _participant(context, body, write=True)}


async def handle_participant_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return {
        "value": await _participant(
            context,
            {"op": "get", "participant_id": params["participant_id"]},
            write=False,
        )
    }


async def handle_participant_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return {"value": await _participant(context, {"op": "list"}, write=False)}


async def handle_scope_put(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Put one memory scope owned by the verified caller."""
    scope = {
        "scope_id": params["scope_id"],
        "tenant": str(context.caller.tenant),
        "name": params["name"],
        "owner_principal": str(context.caller.principal),
        "session_ids": list(params.get("session_ids") or ()),
        "shared_with": list(params.get("shared_with") or ()),
        "schema_version": _SCHEMA_VERSION,
    }
    answer = await _participant(
        context, {"op": "put_scope", "scope": scope}, write=True
    )
    return {"value": answer}


async def handle_observation_set(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Set who a participant observes in a session; the engine checks the caller."""
    body = {"op": "set_observation", "config": dict(params)}
    return {"value": await _participant(context, body, write=True)}


async def handle_turn_append(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Store one conversation turn; the engine enqueues the derive job."""
    seed = f"{context.caller.tenant}|{context.idempotency_key}|{params['session_id']}"
    memory_id = "turn:" + hashlib.sha256(seed.encode()).hexdigest()[:32]
    record = {
        "kind": "conversation_turn",
        "memory_id": memory_id,
        "tenant": str(context.caller.tenant),
        "text": params["text"],
        "tier": "episodic",
        "session_id": params["session_id"],
        "speaker": params["speaker"],
        "timestamp_ms": params["timestamp_ms"],
    }
    derive: Literal["enqueued", "deferred"] = "enqueued"
    try:
        await engine_send(
            context, "MemoryRemember", {"request": {"record": record}}, idempotent=True
        )
    except EngineRefusal as refusal:
        if refusal.code != "DERIVE_BACKLOG_FULL":
            raise
        # The turn is committed; only its derive job waits for room.
        derive = "deferred"
    return TurnResult(
        turn_committed=True,
        memory_id=memory_id,
        derive=derive,
    ).model_dump(mode="json")


async def handle_participant_ask(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Ask one participant's memory; the answer cites claims or abstains."""
    question: dict[str, Any] = {
        "subject": params["subject"],
        "question": params["question"],
        "observer": _observer(params.get("observer", "system")),
    }
    if params.get("memory_scope"):
        question["memory_scope"] = {"scope_id": params["memory_scope"]}
    if params.get("as_of_ms") is not None:
        question["as_of_ms"] = params["as_of_ms"]
    answer = await _participant(
        context, {"op": "ask", "question": question}, write=False
    )
    return {"value": answer}


async def handle_participant_card(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One participant's card as one observer sees it."""
    body = {
        "op": "card",
        "subject": params["subject"],
        "observer": _observer(params.get("observer", "system")),
    }
    return {"value": await _participant(context, body, write=False)}


class DerivationStatusParams(Params):
    scope_id: str | None = Field(default=None, min_length=1, max_length=256)


async def handle_derivation_status(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The pending derivation backlog per scope, session and subject (engine rows)."""
    body: dict[str, Any] = {"op": "status"}
    if params.get("scope_id"):
        body["memory_scope"] = {"scope_id": params["scope_id"]}
    return {"value": await _participant(context, body, write=False)}


def operations() -> tuple[OpSpec, ...]:
    h = "arbora.api.ops.participants.handle_"
    writes = (
        (
            "participant.put",
            "Create or replace one participant",
            "add a person to the workspace",
            ParticipantPutParams,
            "participant_put",
        ),
        (
            "participant.member.add",
            "Add a participant to a session",
            "add this participant to the session",
            MemberAddParams,
            "member_add",
        ),
        (
            "participant.scope.put",
            "Put one memory scope",
            "create a shared memory scope",
            ScopePutParams,
            "scope_put",
        ),
        (
            "participant.observation.set",
            "Set who a participant observes in a session",
            "let this participant observe others",
            ObservationSetParams,
            "observation_set",
        ),
    )
    built = [
        build_write_op(
            verb=Verb.WRITE,
            op_id=op_id,
            summary=summary,
            examples=(example,),
            params=model,
            result=EngineReply,
            handler=h + handler,
            scope=WRITE_SCOPE,
        )
        for op_id, summary, example, model, handler in writes
    ]
    built.append(
        build_write_op(
            verb=Verb.WRITE,
            op_id="participant.turn.append",
            summary="Append one conversation turn to participant memory",
            examples=("remember what was just said",),
            params=TurnAppendParams,
            result=TurnResult,
            handler=h + "turn_append",
            scope=WRITE_SCOPE,
        )
    )
    reads = (
        (
            "participant.get",
            Verb.ASK,
            "Read one participant",
            "show this participant",
            ParticipantGetParams,
            "participant_get",
        ),
        (
            "participant.list",
            Verb.FIND,
            "List the workspace's participants",
            "list the participants",
            ParticipantListParams,
            "participant_list",
        ),
        (
            "participant.ask",
            Verb.ASK,
            "Ask what one participant's memory holds",
            "what do we know about this person",
            ParticipantAskParams,
            "participant_ask",
        ),
        (
            "participant.card",
            Verb.ASK,
            "Read one participant's memory card",
            "show this person's card",
            ParticipantCardParams,
            "participant_card",
        ),
        (
            "participant.derivation.status",
            Verb.ASK,
            "Show the pending memory derivation backlog",
            "how far behind is memory derivation",
            DerivationStatusParams,
            "derivation_status",
        ),
    )
    built.extend(
        build_read_op(
            verb=verb,
            op_id=op_id,
            summary=summary,
            examples=(example,),
            params=model,
            result=EngineReply,
            handler=h + handler,
            scope=READ_SCOPE,
        )
        for op_id, verb, summary, example, model, handler in reads
    )
    return tuple(built)


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "handle_member_add",
    "handle_observation_set",
    "handle_participant_ask",
    "handle_participant_card",
    "handle_participant_get",
    "handle_participant_list",
    "handle_derivation_status",
    "handle_participant_put",
    "handle_scope_put",
    "handle_turn_append",
    "operations",
    "specs",
]
