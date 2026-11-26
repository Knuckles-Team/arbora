"""Served memory operations over the engine KV modality (ARBORA-OPS-R844..R847).

``memory.read``, ``memory.write``, ``memory.list`` and ``memory.delete`` map
onto EG's versioned KV methods (``KvGet``, ``KvPut``/``KvCas``, ``KvScan``,
``KvDelete``). Every call runs under the caller's own verified session, so
EG scopes each entry to the calling principal. ``scope="shared"`` selects
EG's shared namespace instead; writing or deleting there additionally needs
``kv:shared:write``.

A memory key lives at ``memory/<namespace>/<key>``: the EG namespace is
``memory`` and the EG key is ``<namespace>/<key>``, so ``memory.list`` is a
prefix scan of one namespace. Values are JSON objects; every result carries
the entry's EG version.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.memory_files import (
    MemoryFilesList,
    MemoryFilesRead,
    MemoryFilesSave,
)
from arbora.api.registry import ContentOrigin, OpSpec

#: The one EG KV namespace that holds every memory entry.
EG_NAMESPACE = "memory"
#: The extra scope EG requires to write the shared namespace.
SHARED_WRITE_SCOPE = "kv:shared:write"
_NAME = r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$"
_MAX_VALUE_BYTES = 262_144

Scope = Literal["principal", "shared"]

#: EG replays each KV write by its idempotency key (``OperationIdentity``),
#: so the engine owns these operations' replay.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "memory.files.save": "medulla:SyncMemorySnapshot",
    "memory.write": "medulla:KvPut/KvCas",
    "memory.delete": "medulla:KvDelete",
}


class _Located(Params):
    namespace: str = Field(default="default", pattern=_NAME)
    scope: Scope = "principal"


class MemoryKeyParams(_Located):
    key: str = Field(pattern=_NAME)


class MemoryWriteParams(MemoryKeyParams):
    value: dict[str, Any] = Field(default_factory=dict)
    #: Compare-and-set: write only when the entry is at this version
    #: (``0`` = only when absent).
    expected_version: int | None = Field(default=None, ge=0)
    #: ``open_web`` content is long-term memory only after a clean screen
    #: (ARBORA-OPS-R042.4).
    content_origin: ContentOrigin = ContentOrigin.TRUSTED


class MemoryListParams(_Located):
    prefix: str = Field(default="", max_length=128)
    cursor: str | None = Field(default=None, max_length=1_024)
    limit: int = Field(default=50, ge=1, le=200)


class MemoryResult(Params):
    value: dict[str, Any]


def _eg_key(namespace: str, key: str) -> str:
    return f"{namespace}/{key}"


def _located(params: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "namespace": params.get("namespace", "default"),
        "scope": params.get("scope", "principal"),
    }


def _shared(params: Mapping[str, Any]) -> bool:
    return params.get("scope") == "shared"


def _require_shared_write(context: Any, params: Mapping[str, Any]) -> None:
    from arbora.api.invoke.pipeline import OperationRefused

    scopes: frozenset[str] = getattr(context.caller, "effective_scopes", frozenset())
    if _shared(params) and SHARED_WRITE_SCOPE not in scopes:
        raise OperationRefused(
            "SCOPE_REQUIRED", {"missing_scopes": [SHARED_WRITE_SCOPE]}
        )


def _encode(value: Mapping[str, Any]) -> bytes:
    from arbora.api.invoke.pipeline import OperationRefused

    raw = json.dumps(dict(value), sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > _MAX_VALUE_BYTES:
        raise OperationRefused("INVALID_ARGUMENT", {})
    return raw


def _decode(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            value = json.loads(bytes(raw))
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {"value": value}
    return raw if isinstance(raw, dict) else {}


def _payload(result: Any) -> Any:
    return getattr(result, "payload", result)


async def _send(context: Any, method: str, params: dict[str, Any], key: str | None):
    """Send one KV method under the caller's own verified session."""
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.mcp_server import runtime
    from arbora.security.session import use_session

    engine = runtime._get_engine() if callable(runtime._get_engine) else None
    if engine is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "engine is not bound"})
    # The served engine routes each request to the ambient session's graph.
    routed = getattr(engine, "_session_client", None)
    client = routed() if callable(routed) else engine
    session = getattr(context.caller, "session", None)
    if session is None:
        return await client._send(method, params, None, idempotency_key=key)
    with use_session(session):
        return await client._send(method, params, None, idempotency_key=key)


async def handle_memory_read(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Read one entry; an absent key is ``found: false``, not an error."""
    key = params["key"]
    entry = _payload(
        await _send(
            context,
            "KvGet",
            {
                "namespace": EG_NAMESPACE,
                "key": _eg_key(params.get("namespace", "default"), key),
                "shared": _shared(params),
            },
            None,
        )
    )
    found = isinstance(entry, Mapping) and "value" in entry
    return {
        "value": {
            "key": key,
            **_located(params),
            "found": found,
            "value": _decode(entry["value"]) if found else {},
            "version": int(entry.get("version", 0)) if found else 0,
        }
    }


async def handle_memory_write(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Put one entry, or compare-and-set it when ``expected_version`` is given."""
    _require_shared_write(context, params)
    key = params["key"]
    target = {
        "namespace": EG_NAMESPACE,
        "key": _eg_key(params.get("namespace", "default"), key),
        "shared": _shared(params),
    }
    encoded = _encode(params.get("value") or {})
    expected = params.get("expected_version")
    if expected is None:
        receipt = _payload(
            await _send(
                context, "KvPut", {**target, "value": encoded}, context.idempotency_key
            )
        )
        swapped = True
    else:
        receipt = _payload(
            await _send(
                context,
                "KvCas",
                {**target, "expected_version": int(expected), "new": encoded},
                context.idempotency_key,
            )
        )
        swapped = bool(isinstance(receipt, Mapping) and receipt.get("swapped"))
    version = int(receipt.get("version", 0)) if isinstance(receipt, Mapping) else 0
    return {
        "value": {
            "key": key,
            **_located(params),
            "swapped": swapped,
            "version": version,
        }
    }


async def handle_memory_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """List one namespace's entries (optionally under a key prefix)."""
    namespace = params.get("namespace", "default")
    base = f"{namespace}/"
    request: dict[str, Any] = {
        "namespace": EG_NAMESPACE,
        "prefix": base + str(params.get("prefix") or ""),
        "limit": int(params.get("limit") or 50),
        "shared": _shared(params),
    }
    if params.get("cursor"):
        request["cursor"] = params["cursor"]
    page = _payload(await _send(context, "KvScan", request, None))
    rows = page.get("entries", []) if isinstance(page, Mapping) else []
    entries = [
        {
            "key": str(row.get("key", "")).removeprefix(base),
            "value": _decode(row.get("value")),
            "version": int(row.get("version", 0)),
        }
        for row in rows
        if isinstance(row, Mapping)
    ]
    return {
        "value": {
            **_located(params),
            "entries": entries,
            "next_cursor": page.get("next_cursor")
            if isinstance(page, Mapping)
            else None,
        }
    }


async def handle_memory_delete(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Delete one entry; ``deleted`` reports whether it existed."""
    _require_shared_write(context, params)
    key = params["key"]
    deleted = await _send(
        context,
        "KvDelete",
        {
            "namespace": EG_NAMESPACE,
            "key": _eg_key(params.get("namespace", "default"), key),
            "shared": _shared(params),
        },
        context.idempotency_key,
    )
    return {
        "value": {"key": key, **_located(params), "deleted": bool(_payload(deleted))}
    }


async def _decoded(result: Any, method: str, decoder: Any) -> dict[str, Any]:
    from medulla.generated._runtime import OpaqueResult

    return decoder(OpaqueResult(method, _payload(result))).model_dump(
        mode="json", exclude_none=False
    )


async def handle_memory_files_read(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Read one document head or retained revision, with its head facts."""
    from medulla.generated.graph import decode_read_memory_snapshot

    request: dict[str, Any] = {"doc_id": params["doc_id"]}
    if params.get("revision") is not None:
        request["revision"] = params["revision"]
    result = await _send(context, "ReadMemorySnapshot", request, None)
    return {
        "value": await _decoded(
            result, "ReadMemorySnapshot", decode_read_memory_snapshot
        )
    }


async def handle_memory_files_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """List document heads under a prefix; the cursor passes through unchanged."""
    from medulla.generated.graph import decode_list_memory_snapshots

    request: dict[str, Any] = {
        "prefix": params.get("prefix", ""),
        "limit": min(200, max(1, int(params.get("limit", 50)))),
    }
    if params.get("cursor") is not None:
        request["cursor"] = params["cursor"]
    result = await _send(context, "ListMemorySnapshots", request, None)
    return {
        "value": await _decoded(
            result, "ListMemorySnapshots", decode_list_memory_snapshots
        )
    }


async def handle_memory_files_save(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Submit or resolve one document. The engine decides; Arbora never merges.

    After the engine accepts or resolves a head, the projection is written to
    the hosted directory and then acknowledged (ARBORA-OPS-R024.4.4).
    """
    from medulla.generated.graph import (
        decode_read_memory_snapshot,
        decode_sync_memory_snapshot,
    )

    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.api.ops.memory_files import digest_matches, sync_outcome, sync_request

    materializer = context.services.get("memory_materializer")
    if materializer is None:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "memory file materializer is not bound"}
        )
    materializer.root()
    content, digest = params.get("content"), params.get("content_digest")
    if content is not None and not digest_matches(content, digest or ""):
        raise OperationRefused("INVALID_ARGUMENT", {"field": "content_digest"})
    materializer.path_for(params["doc_id"])
    key = context.idempotency_key or ""
    result = await _send(context, "SyncMemorySnapshot", sync_request(params, key), key)
    raw = await _decoded(result, "SyncMemorySnapshot", decode_sync_memory_snapshot)
    value = sync_outcome(raw)
    if raw["outcome"] not in ("accepted", "resolved"):
        return {"value": value}
    head = raw["head"]
    text = content
    if text is None or raw["outcome"] == "resolved":
        read = await _send(
            context, "ReadMemorySnapshot", {"doc_id": params["doc_id"]}, None
        )
        text = (
            await _decoded(read, "ReadMemorySnapshot", decode_read_memory_snapshot)
        )["content"]
    value["materialized"] = False
    if materializer.write(params["doc_id"], text):
        try:
            await _send(
                context,
                "SyncMemorySnapshot",
                materializer.acknowledgment(params["doc_id"], head, key),
                f"{key}:ack",
            )
            value["materialized"] = True
        except Exception as error:  # noqa: BLE001 - the head stands; the report is retried
            logging.getLogger(__name__).warning(
                "memory file acknowledgment failed: %s", type(error).__name__
            )
    return {"value": value}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="memory.files.read",
            summary="Read one memory file head or retained revision",
            examples=("read this memory file", "open my notes document"),
            params=MemoryFilesRead,
            result=MemoryResult,
            handler="arbora.api.ops.memory.handle_memory_files_read",
            scope="memory:read",
        ),
        build_read_op(
            op_id="memory.files.list",
            summary="List memory file heads under a prefix",
            examples=("list my memory files", "which notes documents exist"),
            params=MemoryFilesList,
            result=MemoryResult,
            handler="arbora.api.ops.memory.handle_memory_files_list",
            scope="memory:read",
        ),
        build_write_op(
            op_id="memory.files.save",
            summary="Save a memory file revision or resolve its conflict",
            examples=("save this memory file", "resolve the conflict in my notes"),
            params=MemoryFilesSave,
            result=MemoryResult,
            handler="arbora.api.ops.memory.handle_memory_files_save",
            scope="memory:write",
        ),
        build_read_op(
            op_id="memory.read",
            summary="Read one of the caller's memory entries with its version",
            examples=("read this memory key", "what did I remember about this"),
            params=MemoryKeyParams,
            result=MemoryResult,
            handler="arbora.api.ops.memory.handle_memory_read",
            scope="kv:read",
        ),
        build_read_op(
            op_id="memory.list",
            summary="List the caller's memory entries in one namespace",
            examples=("list my memories", "show remembered notes"),
            params=MemoryListParams,
            result=MemoryResult,
            handler="arbora.api.ops.memory.handle_memory_list",
            scope="kv:read",
        ),
        build_write_op(
            op_id="memory.write",
            summary="Remember one entry for the caller, optionally compare-and-set",
            examples=("remember this for later", "save this note to memory"),
            params=MemoryWriteParams,
            result=MemoryResult,
            handler="arbora.api.ops.memory.handle_memory_write",
            scope="kv:write",
        ),
        build_write_op(
            op_id="memory.delete",
            summary="Forget one of the caller's memory entries",
            examples=("forget this memory", "delete this remembered note"),
            params=MemoryKeyParams,
            result=MemoryResult,
            handler="arbora.api.ops.memory.handle_memory_delete",
            scope="kv:write",
        ),
    )


specs = operations

__all__ = [
    "EG_NAMESPACE",
    "NATIVE_IDEMPOTENCY",
    "SHARED_WRITE_SCOPE",
    "MemoryKeyParams",
    "MemoryListParams",
    "MemoryResult",
    "MemoryWriteParams",
    "handle_memory_delete",
    "handle_memory_files_list",
    "handle_memory_files_read",
    "handle_memory_files_save",
    "handle_memory_list",
    "handle_memory_read",
    "handle_memory_write",
    "operations",
    "specs",
]
