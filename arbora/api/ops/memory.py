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
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.registry import OpSpec

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
    "memory.write": "epistemic_graph:KvPut/KvCas",
    "memory.delete": "epistemic_graph:KvDelete",
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
    from arbora.api.contracts.session import use_session
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.mcp_server import runtime

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
            "next_cursor": page.get("next_cursor") if isinstance(page, Mapping) else None,
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
    return {"value": {"key": key, **_located(params), "deleted": bool(_payload(deleted))}}


def operations() -> tuple[OpSpec, ...]:
    return (
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
    "handle_memory_list",
    "handle_memory_read",
    "handle_memory_write",
    "operations",
    "specs",
]
