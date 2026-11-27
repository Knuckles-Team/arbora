"""Chat history scoped to the caller (ARBORA-OPS-R909).

A chat is one versioned memory file of the engine, ``chats/<subject>/<chat_id>``
(ARBORA-OPS-R024.4), so the engine holds the facts and Arbora keeps none. The
subject is the verified principal; a request never names an owner, and another
subject's id is simply absent: the document id is built from the caller, so a
foreign chat answers exactly like a missing one. A delete writes a tombstone
head, because the memory file contract has no removal; a tombstone reads as
absent.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.engine_calls import engine_send, rfc3339
from arbora.api.ops.knowledge import EngineReply
from arbora.api.registry import OpSpec, Verb

SCOPE = "chats:own"
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "chats.create": "medulla:SyncMemorySnapshot",
    "chats.update": "medulla:SyncMemorySnapshot",
    "chats.delete": "medulla:SyncMemorySnapshot",
}
#: A message body is at most 256 KiB; a list page at most 100 rows.
MAX_MESSAGE_BYTES = 256 * 1024
MAX_PAGE = 100
_Id = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
_Messages = Field(default=None, max_length=10_000)


class ChatsListParams(Params):
    cursor: str | None = Field(default=None, max_length=1024)
    limit: int = Field(default=50, ge=1, le=MAX_PAGE)


class ChatGetParams(Params):
    chat_id: str = _Id


class ChatCreateParams(Params):
    title: str | None = Field(default=None, max_length=512)
    messages: list[dict[str, Any]] | None = _Messages


class ChatUpdateParams(Params):
    chat_id: str = _Id
    title: str | None = Field(default=None, max_length=512)
    messages: list[dict[str, Any]] | None = _Messages
    expected_revision: int = Field(ge=0)


def now() -> datetime:
    """The stamp clock; tests replace it."""
    return datetime.now(UTC)


def _prefix(context: Any) -> str:
    return f"chats/{context.caller.principal}/"


def _doc_id(context: Any, chat_id: str) -> str:
    return _prefix(context) + chat_id


def _stamp() -> str:
    return rfc3339(int(now().timestamp() * 1000))


def _encode(chat: Mapping[str, Any]) -> tuple[str, str]:
    content = json.dumps(chat, sort_keys=True, separators=(",", ":"))
    return content, hashlib.sha256(content.encode()).hexdigest()


def _check_messages(messages: list[dict[str, Any]] | None) -> None:
    for message in messages or ():
        body = json.dumps(message, separators=(",", ":"))
        if len(body.encode()) > MAX_MESSAGE_BYTES:
            raise OperationRefused("INVALID_ARGUMENT", {"field": "messages"})


def _absent() -> OperationRefused:
    return OperationRefused("NOT_FOUND")


async def _read(context: Any, chat_id: str) -> tuple[dict[str, Any], int]:
    """The live chat and its head revision; a tombstone or foreign id is absent."""
    try:
        answer = await engine_send(
            context, "ReadMemorySnapshot", {"doc_id": _doc_id(context, chat_id)}
        )
    except Exception as exc:  # noqa: BLE001 - absent and refused read the same
        if getattr(exc, "code", "") in ("NOT_FOUND", "MEMORY_NOT_FOUND", "FORBIDDEN"):
            raise _absent() from exc
        raise
    if not isinstance(answer, Mapping) or not isinstance(answer.get("content"), str):
        raise _absent()
    chat = json.loads(answer["content"])
    if chat.get("deleted") is True:
        raise _absent()
    return chat, int(answer["head"]["revision"])


async def _save(
    context: Any, chat_id: str, chat: Mapping[str, Any], expected: int | None
) -> int:
    content, digest = _encode(chat)
    action: dict[str, Any] = {
        "action": "submit",
        "content": content,
        "content_digest": digest,
    }
    if expected is not None:
        action["expected_revision"] = expected
    key = context.idempotency_key or ""
    answer = await engine_send(
        context,
        "SyncMemorySnapshot",
        {
            "request": {
                "action": action,
                "doc_id": _doc_id(context, chat_id),
                "request_id": key,
            }
        },
        idempotent=True,
    )
    outcome = answer.get("outcome") if isinstance(answer, Mapping) else None
    if outcome == "accepted":
        return int(answer["head"]["revision"])
    if outcome == "conflict_retained":
        raise OperationRefused("REVISION_CONFLICT")
    raise _absent()


def _view(chat_id: str, chat: Mapping[str, Any], revision: int) -> dict[str, Any]:
    return {
        "id": chat_id,
        "title": chat.get("title") or "",
        "messages": chat.get("messages") or [],
        "updated_at": chat.get("updated_at"),
        "revision": revision,
    }


async def handle_chats_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "prefix": _prefix(context),
        "limit": min(MAX_PAGE, int(params.get("limit", 50))),
    }
    if params.get("cursor") is not None:
        request["cursor"] = params["cursor"]
    page = await engine_send(context, "ListMemorySnapshots", request)
    prefix = _prefix(context)
    chats = []
    for head in page.get("heads", ()):
        chat_id = str(head["doc_id"])[len(prefix) :]
        try:
            chat, _ = await _read(context, chat_id)
        except OperationRefused:
            continue
        chats.append(
            {
                "id": chat_id,
                "title": chat.get("title") or "",
                "updated_at": chat.get("updated_at"),
            }
        )
    return {"value": {"chats": chats, "next_cursor": page.get("next_cursor")}}


async def handle_chats_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    chat, revision = await _read(context, params["chat_id"])
    return {"value": {"chat": _view(params["chat_id"], chat, revision)}}


async def handle_chats_create(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    _check_messages(params.get("messages"))
    # The id derives from the idempotency key, so a replayed create is one chat.
    key = context.idempotency_key or uuid.uuid4().hex
    chat_id = hashlib.sha256(f"{context.caller.principal}:{key}".encode()).hexdigest()[
        :32
    ]
    chat = {
        "title": params.get("title") or "",
        "messages": list(params.get("messages") or ()),
        "updated_at": _stamp(),
    }
    revision = await _save(context, chat_id, chat, 0)
    return {"value": {"chat": _view(chat_id, chat, revision)}}


async def handle_chats_update(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    _check_messages(params.get("messages"))
    chat, _ = await _read(context, params["chat_id"])
    if params.get("title") is not None:
        chat["title"] = params["title"]
    if params.get("messages") is not None:
        chat["messages"] = list(params["messages"])
    chat["updated_at"] = _stamp()
    revision = await _save(
        context, params["chat_id"], chat, params["expected_revision"]
    )
    return {"value": {"chat": _view(params["chat_id"], chat, revision)}}


async def handle_chats_delete(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    _, revision = await _read(context, params["chat_id"])
    await _save(
        context, params["chat_id"], {"deleted": True, "updated_at": _stamp()}, revision
    )
    return {"value": {"deleted": True}}


def operations() -> tuple[OpSpec, ...]:
    h = "arbora.api.ops.chats.handle_"

    def read(op_id: str, verb: Verb, summary: str, model: Any, handler: str) -> OpSpec:
        return build_read_op(
            verb=verb,
            op_id=op_id,
            summary=summary,
            examples=(summary.lower(),),
            params=model,
            result=EngineReply,
            handler=h + handler,
            scope=SCOPE,
        )

    def write(op_id: str, summary: str, model: Any, handler: str) -> OpSpec:
        return build_write_op(
            verb=Verb.WRITE,
            op_id=op_id,
            summary=summary,
            examples=(summary.lower(),),
            params=model,
            result=EngineReply,
            handler=h + handler,
            scope=SCOPE,
        )

    return (
        read("chats.list", Verb.FIND, "List my chats", ChatsListParams, "chats_list"),
        read("chats.get", Verb.ASK, "Read one of my chats", ChatGetParams, "chats_get"),
        write("chats.create", "Start a chat", ChatCreateParams, "chats_create"),
        write(
            "chats.update", "Update one of my chats", ChatUpdateParams, "chats_update"
        ),
        write("chats.delete", "Delete one of my chats", ChatGetParams, "chats_delete"),
    )


specs = operations
