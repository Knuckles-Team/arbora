"""Surface a recommendation only after a passing safety-bound verdict (ARBORA-OPS-R862).

The one result gate of the engine ``Decide`` answers, called at the invoke result
chokepoint so MCP, HTTP and A2A all pass it. It reads the sealed record's safety verdict
and writes nothing: ``Pass`` surfaces the record, ``Withheld`` and an absent verdict
refuse ``POLICY_DENIED`` with a reason.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arbora.security.operation_result import OpError, OpResult

DECIDE_OPS = frozenset({"eg.query.Decide", "eg.query.DecideText"})


def _verdict(record: Mapping[str, Any]) -> tuple[str | None, Any]:
    """``(verdict, bound id)`` of a record's ``safety_verdict``; ``(None, None)`` if absent."""
    raw = record.get("safety_verdict")
    if isinstance(raw, str):
        return raw, record.get("bound_id")
    if isinstance(raw, Mapping):
        if "verdict" in raw:
            return str(raw["verdict"]), raw.get("bound_id")
        for name, body in raw.items():
            bound = body.get("bound_id") if isinstance(body, Mapping) else None
            return str(name), bound
    return None, None


def gate_recommendation(record: Any) -> Any | OpError:
    """The record when its verdict is ``Pass``; otherwise a ``POLICY_DENIED`` refusal."""
    if isinstance(record, (list, tuple)):
        for item in record:
            blocked = gate_recommendation(item)
            if isinstance(blocked, OpError):
                return blocked
        return record
    if not isinstance(record, Mapping):
        return OpError("POLICY_DENIED", {"reason": "safety_verdict_absent"})
    verdict, bound = _verdict(record)
    if verdict is None:
        return OpError("POLICY_DENIED", {"reason": "safety_verdict_absent"})
    if verdict == "Pass":
        return record
    return OpError(
        "POLICY_DENIED", {"reason": "recommendation_withheld", "bound_id": bound}
    )


def gate_result(op_id: str, result: OpResult | OpError) -> OpResult | OpError:
    """Apply the gate to the result of a ``Decide`` operation; other results pass."""
    if op_id not in DECIDE_OPS or isinstance(result, OpError):
        return result
    gated = gate_recommendation(result.value)
    return gated if isinstance(gated, OpError) else result


__all__ = ["DECIDE_OPS", "gate_recommendation", "gate_result"]
