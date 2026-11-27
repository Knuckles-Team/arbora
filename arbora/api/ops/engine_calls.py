"""One engine call for composite handlers that read or write through the engine.

The caller's verified graph and claims bound every send, and an engine refusal
keeps its own code (ARBORA-OPS-R920). The engine names a refusal either in
``code`` or as the leading token of its detail (``DECISION_NOT_FOUND: ...``);
both read the engine's own vocabulary and never a message Arbora wrote.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import msgpack

from arbora.api.engine_errors import ENGINE_ERRORS
from arbora.api.errors import EngineRefusal
from arbora.api.invoke.bootstrap import caller_dispatch_graph
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import claim_values
from arbora.security.anonymous import client_scope


def refusal_code(code: str, detail: str | None) -> str:
    """The engine code a refusal carries: a named leading token wins."""
    leading = (detail or "").split(":", 1)[0].strip()
    return leading if leading in ENGINE_ERRORS else code


def decode(payload: Any) -> Any:
    """An engine result as plain data: MessagePack bytes are unpacked."""
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return msgpack.unpackb(bytes(payload), raw=False)
    return payload


def node_properties(blob: Any) -> dict[str, Any]:
    """One node's properties from the engine's MessagePack blob (or an already-plain map)."""
    if isinstance(blob, Mapping):
        return dict(blob)
    if isinstance(blob, (bytes, bytearray, memoryview)):
        loaded = msgpack.unpackb(bytes(blob), raw=False)
        return loaded if isinstance(loaded, dict) else {}
    return {}


def node_rows(raw: Any) -> list[tuple[str, dict[str, Any]]]:
    """``(node id, properties)`` of each present node an engine node read answered."""
    rows: list[tuple[str, dict[str, Any]]] = []
    for row in decode(raw) or ():
        if isinstance(row, (list, tuple)) and len(row) == 2 and row[1] is not None:
            rows.append((str(row[0]), node_properties(row[1])))
    return rows


async def engine_send(
    context: Any, method: str, params: dict[str, Any], *, idempotent: bool = False
) -> Any:
    """Send one engine method as the verified caller and return plain data."""
    _require_method(method)
    graph = await caller_dispatch_graph(context)
    key = context.idempotency_key if idempotent else None
    with client_scope(context.client, claim_values(context.caller.engine_claims)):
        return await send_on_client(context.client, method, params, graph, key=key)


def _require_method(method: str) -> None:
    from medulla.generated import METHOD_IDS

    if method not in METHOD_IDS:
        # A newer engine release declares the method; until it is pinned the
        # operation refuses rather than sending a method the client cannot.
        raise unavailable(f"the installed engine does not serve {method}")


async def send_on_client(
    client: Any, method: str, params: dict[str, Any], graph: str, *, key: str | None
) -> Any:
    """Send one engine method on a client whose claims are already bound."""
    from medulla import EngineResponseError
    from medulla.generated import send_by_id

    _require_method(method)
    try:
        payload = await send_by_id(client, method, params, graph, idempotency_key=key)
    except EngineResponseError as exc:
        raise EngineRefusal(refusal_code(exc.code, exc.detail)) from exc
    return decode(payload)


async def engine_graph_op(
    context: Any, method: str, op: dict[str, Any], *, idempotent: bool = False
) -> Any:
    """Send one ``{op: ...}`` engine method with the caller's graph in the op."""
    graph = await caller_dispatch_graph(context)
    return await engine_send(
        context, method, {"op": {**op, "graph": graph}}, idempotent=idempotent
    )


def unavailable(reason: str) -> OperationRefused:
    """The engine answered with a shape the contract does not declare."""
    return OperationRefused("UNAVAILABLE", {"reason": reason})


def rfc3339(millis: int) -> str:
    """Epoch milliseconds as an RFC 3339 UTC instant."""
    moment = datetime.fromtimestamp(millis / 1000, tz=UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
