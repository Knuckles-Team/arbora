"""Engine ``Decide`` questions and committed records for the gate operations.

One place builds the ``Decide`` request of a question kind that medulla ``main`` declares
and reads a committed record back from the decision log. Arbora decides nothing: it sends
the question as the verified caller and reads the sealed record (ARBORA-OPS-R862 to R877).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from arbora.api.ops._engine import engine_send
from arbora.pin_lag.medulla import guard


def record_of(answer: Any) -> dict[str, Any]:
    """The one sealed record of an engine answer (a record or a one-record batch)."""
    if isinstance(answer, (list, tuple)):
        answer = answer[0] if answer else {}
    return dict(answer) if isinstance(answer, Mapping) else {}


#: The engine ``QuestionKind`` wire form: snake_case, never the Rust variant name.
_WIRE_KIND = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")


def require_wire_kind(kind: str) -> str:
    """``kind`` when it is a snake_case wire kind, else ``ValueError``."""
    if _WIRE_KIND.fullmatch(kind) is None:
        raise ValueError(f"a Decide question kind is snake_case on the wire: {kind!r}")
    return kind


def record_digest(record: Mapping[str, Any]) -> str:
    """SHA-256 of the record's canonical JSON."""
    body = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def decide_with(
    send: Callable[[str, dict[str, Any]], Awaitable[Any]],
    tenant: str,
    kind: str,
    fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask one ``QuestionKind`` over ``send(method, params)`` and return its sealed record."""
    guard("Decide")
    require_wire_kind(kind)
    request = {"tenant_id": tenant, "question": {"kind": kind, **dict(fields or {})}}
    return record_of(await send("Decide", {"request": request}))


async def decide_record(
    context: Any, kind: str, fields: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Ask one ``QuestionKind`` as the verified caller and return its sealed record."""

    async def send(method: str, params: dict[str, Any]) -> Any:
        return await engine_send(context, method, params)

    return await decide_with(send, str(context.caller.tenant), kind, fields)


async def committed_record(context: Any, record_id: str) -> dict[str, Any]:
    """One committed decision record, read from the decision log."""
    guard("DecisionLog")
    op = {"op": "get", "record_id": record_id, "tenant_id": str(context.caller.tenant)}
    return record_of(await engine_send(context, "DecisionLog", {"op": op}))


__all__ = [
    "committed_record",
    "decide_record",
    "decide_with",
    "record_digest",
    "record_of",
    "require_wire_kind",
]
