"""Served workstream operations (ARBORA-HARNESS-PLUGINS-R004).

One workstream joins the harness sessions of one principal on one workspace.
The record lives in the engine (``MEDULLA-IDENTITY-R723``): a ``Workstream``
node, one ``client_session`` node per harness session and an ``attachedTo``
edge between them. Arbora keeps no state and stores the workspace digest,
never the workspace root.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any

import msgpack
from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.ops._engine import decode, engine_send
from arbora.api.registry import Confirm, OpSpec, Verb

READ_SCOPE = "workstream:read"
WRITE_SCOPE = "workstream:write"
#: The attach context is bounded so a hook can inject it directly.
CONTEXT_BYTES = 4096
_NAME = r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$"
_LAST = "last_checkpoint"


class AttachmentState(StrEnum):
    DETACHED = "detached"
    ATTACHED = "attached"


class AttachmentEvent(StrEnum):
    ATTACH = "attach"
    CHECKPOINT = "checkpoint"
    DETACH = "detach"


_S, _E = AttachmentState, AttachmentEvent
ATTACH_EDGES: dict[tuple[AttachmentState, AttachmentEvent], AttachmentState] = {
    (_S.DETACHED, _E.ATTACH): _S.ATTACHED,
    (_S.ATTACHED, _E.CHECKPOINT): _S.ATTACHED,
    (_S.ATTACHED, _E.DETACH): _S.DETACHED,
}


class IllegalAttachTransition(RuntimeError):
    code = "ATTACH_TRANSITION_ILLEGAL"

    def __init__(self, state: AttachmentState, event: AttachmentEvent) -> None:
        super().__init__(f"{self.code}: {state} --{event}-->")


def transition(state: AttachmentState, event: AttachmentEvent) -> AttachmentState:
    target = ATTACH_EDGES.get((state, event))
    if target is None:
        raise IllegalAttachTransition(state, event)
    return target


def workspace_digest(principal: str, workspace_root: str) -> str:
    """SHA-256 of the principal and the reported root; the root is never stored."""
    return hashlib.sha256(f"{principal}\0{workspace_root}".encode()).hexdigest()


def workstream_key(tenant: str, principal: str, workspace_root: str) -> str:
    return f"workstream:{tenant}:{workspace_digest(principal, workspace_root)}"


def _session_id(tenant: str, session: str) -> str:
    return f"client_session:{tenant}:{session}"


class AttachParams(_common.Params):
    harness: str = Field(pattern=_NAME)
    harness_session_id: str = Field(pattern=_NAME)
    workspace_root: str = Field(min_length=1, max_length=1024)


class CheckpointParams(_common.Params):
    harness_session_id: str = Field(pattern=_NAME)
    workspace_root: str = Field(min_length=1, max_length=1024)
    summary: str = Field(min_length=1, max_length=2000)
    run_ids: list[str] = Field(default_factory=list, max_length=32)
    trigger: str = Field(default="manual", pattern=_NAME)


class DetachParams(_common.Params):
    harness_session_id: str = Field(pattern=_NAME)


class GetParams(_common.Params):
    workspace_root: str = Field(min_length=1, max_length=1024)


class ListParams(_common.Params):
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=512)


class WorkstreamView(_common.Params):
    value: dict[str, Any]


def _not_found() -> OperationRefused:
    return OperationRefused("WORKSTREAM_NOT_FOUND", {})


def _pack(value: Mapping[str, Any]) -> bytes:
    return msgpack.packb(dict(value), use_bin_type=True)


def _props(blob: Any) -> dict[str, Any] | None:
    if blob is None:
        return None
    if isinstance(blob, Mapping):
        return dict(blob)
    if isinstance(blob, (bytes, bytearray, memoryview)):
        loaded = msgpack.unpackb(bytes(blob), raw=False)
        return loaded if isinstance(loaded, dict) else None
    return None


async def _node(context: Any, node_id: str) -> dict[str, Any] | None:
    rows = decode(
        await engine_send(context, "GetNodePropertiesBatch", {"node_ids": [node_id]})
    )
    for row in rows or ():
        if isinstance(row, (list, tuple)) and len(row) == 2 and row[0] == node_id:
            return _props(row[1])
    return None


async def _owned_workstream(context: Any, key: str) -> dict[str, Any]:
    """The caller's own workstream; a foreign or absent one answers alike."""
    found = await _node(context, key)
    caller = context.caller
    if (
        found is None
        or found.get("tenant") != str(caller.tenant)
        or found.get("principal") != str(caller.principal)
    ):
        raise _not_found()
    return found


def _clock_ms() -> int:
    return int(time.time() * 1000)


def _context_of(workstream: Mapping[str, Any], others: list[str]) -> dict[str, Any]:
    context = {
        _LAST: workstream.get(_LAST, ""),
        "open_run_ids": list(workstream.get("open_run_ids", ())),
        "attached_sessions": others,
    }
    text = repr(context)
    if len(text.encode()) > CONTEXT_BYTES:
        context[_LAST] = str(context[_LAST])[: max(0, CONTEXT_BYTES // 2)]
        context["attached_sessions"] = others[:16]
    return context


async def handle_attach(
    context: Any,
    params: Mapping[str, Any],
    op: OpSpec,
    *,
    clock_ms: Callable[[], int] = _clock_ms,
) -> dict[str, Any]:
    caller = context.caller
    tenant, principal = str(caller.tenant), str(caller.principal)
    key = workstream_key(tenant, principal, params["workspace_root"])
    session = _session_id(tenant, params["harness_session_id"])
    existing = await _node(context, session)
    if existing is not None and existing.get("principal") != principal:
        raise _not_found()
    state = AttachmentState(existing.get("state", "detached")) if existing else None
    now = clock_ms()
    workstream = await _node(context, key)
    if workstream is None:
        workstream = {
            "tenant": tenant,
            "principal": principal,
            "workspace_digest": key.rsplit(":", 1)[1],
            "opened_at_ms": now,
            "last_active_ms": now,
        }
        await engine_send(
            context,
            "AddNode",
            {
                "node_id": key,
                "properties_msgpack": _pack({"labels": ["Workstream"], **workstream}),
            },
            idempotent=True,
        )
    if (
        existing is not None
        and state is AttachmentState.ATTACHED
        and existing.get("workstream") == key
    ):
        return {
            "value": {"workstream": key, "status": "already_attached", "context": {}}
        }
    transition(state or AttachmentState.DETACHED, AttachmentEvent.ATTACH)
    session_props = {
        "principal": principal,
        "tenant": tenant,
        "state": AttachmentState.ATTACHED.value,
        "harness": params["harness"],
        "workstream": key,
        "attached_at_ms": now,
        "detached_at_ms": 0,
    }
    if existing is None:
        await engine_send(
            context,
            "AddNode",
            {
                "node_id": session,
                "properties_msgpack": _pack(
                    {"labels": ["ClientSession"], **session_props}
                ),
            },
            idempotent=True,
        )
        await engine_send(
            context,
            "AddEdge",
            {
                "source_id": session,
                "target_id": key,
                "properties_msgpack": _pack(
                    {"relationship": "attachedTo", "harness": params["harness"]}
                ),
            },
            idempotent=True,
        )
    else:
        await engine_send(
            context,
            "CompareAndSetNodeFields",
            {
                "node_id": session,
                "conditions_msgpack": _pack({"state": existing.get("state")}),
                "updates_msgpack": _pack(
                    {
                        k: session_props[k]
                        for k in (
                            "state",
                            "harness",
                            "attached_at_ms",
                            "detached_at_ms",
                        )
                    }
                ),
            },
        )
    others = [
        str(source)
        for source in decode(
            await engine_send(context, "GetPredecessors", {"node_id": key})
        )
        if source != session
    ]
    return {
        "value": {
            "workstream": key,
            "status": "attached",
            "context": _context_of(workstream, others),
        }
    }


async def handle_checkpoint(
    context: Any,
    params: Mapping[str, Any],
    op: OpSpec,
    *,
    clock_ms: Callable[[], int] = _clock_ms,
) -> dict[str, Any]:
    caller = context.caller
    tenant, principal = str(caller.tenant), str(caller.principal)
    key = workstream_key(tenant, principal, params["workspace_root"])
    session = await _node(context, _session_id(tenant, params["harness_session_id"]))
    if session is None or session.get("principal") != principal:
        raise _not_found()
    transition(
        AttachmentState(session.get("state", "detached")), AttachmentEvent.CHECKPOINT
    )
    workstream = await _owned_workstream(context, key)
    await engine_send(
        context,
        "CompareAndSetNodeFields",
        {
            "node_id": key,
            "conditions_msgpack": _pack(
                {"last_active_ms": workstream.get("last_active_ms")}
            ),
            "updates_msgpack": _pack(
                {
                    _LAST: params["summary"],
                    "open_run_ids": list(params.get("run_ids", ())),
                    "last_trigger": params.get("trigger", "manual"),
                    "last_active_ms": clock_ms(),
                }
            ),
        },
    )
    return {"value": {"workstream": key, "status": "checkpointed"}}


async def handle_detach(
    context: Any,
    params: Mapping[str, Any],
    op: OpSpec,
    *,
    clock_ms: Callable[[], int] = _clock_ms,
) -> dict[str, Any]:
    caller = context.caller
    tenant, principal = str(caller.tenant), str(caller.principal)
    node_id = _session_id(tenant, params["harness_session_id"])
    session = await _node(context, node_id)
    if session is None or session.get("principal") != principal:
        raise _not_found()
    if AttachmentState(session.get("state", "detached")) is AttachmentState.DETACHED:
        return {"value": {"status": "detached"}}
    transition(AttachmentState.ATTACHED, AttachmentEvent.DETACH)
    await engine_send(
        context,
        "CompareAndSetNodeFields",
        {
            "node_id": node_id,
            "conditions_msgpack": _pack({"state": "attached"}),
            "updates_msgpack": _pack(
                {"state": "detached", "detached_at_ms": clock_ms()}
            ),
        },
    )
    return {"value": {"status": "detached"}}


def _view(key: str, node: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "workstream": key,
        "workspace_digest": node.get("workspace_digest"),
        "opened_at_ms": node.get("opened_at_ms"),
        "last_active_ms": node.get("last_active_ms"),
        _LAST: node.get(_LAST, ""),
    }


async def handle_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    caller = context.caller
    key = workstream_key(
        str(caller.tenant), str(caller.principal), params["workspace_root"]
    )
    return {"value": _view(key, await _owned_workstream(context, key))}


async def handle_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    caller = context.caller
    rows = decode(
        await engine_send(
            context,
            "GetNodesByLabel",
            {
                "label": "Workstream",
                "limit": params.get("limit", 50),
                "after": params.get("cursor"),
            },
        )
    )
    items: list[dict[str, Any]] = []
    last: str | None = None
    for row in rows or ():
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            continue
        last = str(row[0])
        node = _props(row[1])
        if (
            node is not None
            and node.get("tenant") == str(caller.tenant)
            and node.get("principal") == str(caller.principal)
        ):
            items.append(_view(str(row[0]), node))
    full = len(rows or ()) >= params.get("limit", 50)
    return {"value": {"workstreams": items, "next_cursor": last if full else None}}


def operations() -> tuple[OpSpec, ...]:
    writes = (
        (
            "workstream.attach",
            "Attach this harness session to the workstream",
            ("attach this session to my workstream",),
            AttachParams,
            handle_attach.__name__,
        ),
        (
            "workstream.checkpoint",
            "Record a handoff checkpoint on the workstream",
            ("checkpoint my workstream",),
            CheckpointParams,
            handle_checkpoint.__name__,
        ),
        (
            "workstream.detach",
            "Detach this harness session from the workstream",
            ("detach this session",),
            DetachParams,
            handle_detach.__name__,
        ),
    )
    built = [
        _common.build_write_op(
            verb=Verb.WRITE,
            op_id=op_id,
            summary=summary,
            examples=examples,
            params=params,
            result=WorkstreamView,
            handler=f"arbora.api.ops.workstreams.{handler}",
            scope=WRITE_SCOPE,
            confirm=Confirm.NONE,
        )
        for op_id, summary, examples, params, handler in writes
    ]
    built.append(
        _common.build_read_op(
            op_id="workstream.get",
            summary="Show one of my workstreams",
            examples=("show my workstream",),
            params=GetParams,
            result=WorkstreamView,
            handler="arbora.api.ops.workstreams.handle_get",
            scope=READ_SCOPE,
        )
    )
    built.append(
        _common.build_read_op(
            verb=Verb.FIND,
            op_id="workstream.list",
            summary="List my workstreams",
            examples=("list my workstreams",),
            params=ListParams,
            result=WorkstreamView,
            handler="arbora.api.ops.workstreams.handle_list",
            scope=READ_SCOPE,
        )
    )
    return tuple(built)


specs = operations

__all__ = [
    "ATTACH_EDGES",
    "AttachmentEvent",
    "AttachmentState",
    "IllegalAttachTransition",
    "handle_attach",
    "handle_checkpoint",
    "handle_detach",
    "handle_get",
    "handle_list",
    "operations",
    "specs",
    "transition",
    "workspace_digest",
    "workstream_key",
]
