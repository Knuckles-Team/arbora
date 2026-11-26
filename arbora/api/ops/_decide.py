"""Engine ``Decide`` questions and committed records for the gate operations.

One place builds the ``Decide`` request of a question kind that medulla ``main`` declares
and reads a committed record back from the decision log. Arbora decides nothing: it sends
the question as the verified caller and reads the sealed record (ARBORA-OPS-R862 to R877).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from arbora.api.ops._engine import engine_send
from arbora.pin_lag.medulla import guard


def record_of(answer: Any) -> dict[str, Any]:
    """The one sealed record of an engine answer (a record or a one-record batch)."""
    if isinstance(answer, (list, tuple)):
        answer = answer[0] if answer else {}
    return dict(answer) if isinstance(answer, Mapping) else {}


def record_digest(record: Mapping[str, Any]) -> str:
    """SHA-256 of the record's canonical JSON."""
    body = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def decide_record(
    context: Any, kind: str, fields: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Ask one ``QuestionKind`` and return its sealed record."""
    guard("Decide")
    request = {
        "tenant_id": str(context.caller.tenant),
        "question": {"kind": kind, **dict(fields or {})},
    }
    return record_of(await engine_send(context, "Decide", {"request": request}))


async def committed_record(context: Any, record_id: str) -> dict[str, Any]:
    """One committed decision record, read from the decision log."""
    guard("DecisionLog")
    op = {"op": "get", "record_id": record_id, "tenant_id": str(context.caller.tenant)}
    return record_of(await engine_send(context, "DecisionLog", {"op": op}))


__all__ = ["committed_record", "decide_record", "record_digest", "record_of"]
