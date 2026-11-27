"""Decision evidence reads (ARBORA-OPS-R920).

``decision.list``, ``decision.provenance.get`` and ``decision.why_not`` forward
the engine's ``DecisionLog`` ``list``, ``provenance`` and ``why_not`` actions
(MEDULLA-DECISION-ENGINE-R870). Arbora holds no decision record, no cache and
no solver: the tenant comes from the verified caller, never from a request
body, and every filter, order, page and bound is the engine's.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops.engine_calls import engine_send, rfc3339, unavailable
from arbora.api.registry import OpSpec

#: The scope the rows name; the engine adds its own decision-read action.
DECISION_READ_SCOPE = "kg:read"
#: The engine's budget field is a 32-bit count of milliseconds.
_BUDGET_MAX_MS = 2**32 - 1


class DecisionListParams(Params):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    question_kind: str | None = Field(default=None, max_length=64)
    resolution_kind: str | None = Field(default=None, max_length=64)
    from_: str | None = Field(default=None, alias="from", max_length=64)
    to: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


class DecisionItem(Params):
    decision_id: str
    question_kind: str | None = None
    resolution_kind: str
    outcome: Any = None
    decided_at: str
    record_digest: str
    inputs_state: str


class DecisionListResult(Params):
    items: list[DecisionItem]
    next_cursor: str | None = None


class DecisionRefParams(Params):
    decision_id: str = Field(min_length=1, max_length=256)


class ProvenancePremise(Params):
    kind: str
    ref: str
    # ``class`` is a keyword; the wire name is kept through the alias.
    premise_class: Any = Field(alias="class")


class ProvenanceFact(Params):
    node_id: str
    revision: int
    digest: str


class ProvenanceRule(Params):
    rule_id: str
    version: int
    digest: str


class DecisionProvenanceResult(Params):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    decision_id: str
    record_digest: str
    premises: list[ProvenancePremise]
    facts: list[ProvenanceFact]
    rules: list[ProvenanceRule]
    approver: str | None = None
    inputs_state: str


class DecisionWhyNotParams(DecisionRefParams):
    option_id: str = Field(
        min_length=1,
        max_length=256,
        description=(
            "The engine's component id of the excluded option: the "
            "`component_id` its decision record names, in the engine's own id "
            "space. Arbora forwards it unchanged and maps no other id."
        ),
    )
    budget_ms: int | None = Field(default=None, ge=0)


class WhyNotReason(Params):
    kind: str
    detail: str


class DecisionWhyNotResult(Params):
    decision_id: str
    option_id: str
    verdict: str
    reasons: list[WhyNotReason]
    budget_used_ms: int
    budget_exhausted: bool


def _millis(value: str | None, field: str) -> int | None:
    """One RFC 3339 instant as epoch milliseconds."""
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise OperationRefused("INVALID_ARGUMENT", {"field": field}) from None
    if moment.tzinfo is None:
        raise OperationRefused("INVALID_ARGUMENT", {"field": field})
    return int(moment.timestamp() * 1000)


async def _log(context: Any, action: dict[str, Any]) -> Mapping[str, Any]:
    """One ``DecisionLog`` read for the verified caller's tenant."""
    body = {**action, "tenant_id": str(context.caller.tenant)}
    result = await engine_send(context, "DecisionLog", {"op": body})
    if not isinstance(result, Mapping):
        raise unavailable("engine result malformed")
    return result


def _item(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise unavailable("engine result malformed")
    try:
        outcome = row.get("outcome")
        return {
            "decision_id": row["record_id"],
            "question_kind": row.get("question_kind"),
            "resolution_kind": row["resolution_kind"],
            "outcome": outcome if outcome is not None else row.get("assembly_outcome"),
            "decided_at": rfc3339(int(row["decided_at_ms"])),
            "record_digest": row["record_digest"],
            "inputs_state": row["inputs"],
        }
    except (KeyError, TypeError, ValueError):
        raise unavailable("engine result malformed") from None


async def handle_decision_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One page of the tenant's decisions, newest first, as the engine pages it."""
    from_ms = _millis(params.get("from_", params.get("from")), "from")
    to_ms = _millis(params.get("to"), "to")
    if from_ms is not None and to_ms is not None and from_ms > to_ms:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "from"})
    action: dict[str, Any] = {"op": "list", "limit": params.get("limit", 50)}
    for key in ("question_kind", "resolution_kind", "cursor"):
        if params.get(key) is not None:
            action[key] = params[key]
    if from_ms is not None:
        action["from_ms"] = from_ms
    if to_ms is not None:
        action["to_ms"] = to_ms
    page = await _log(context, action)
    rows = page.get("records")
    if not isinstance(rows, list):
        raise unavailable("engine result malformed")
    return {
        "items": [_item(row) for row in rows],
        "next_cursor": page.get("next_cursor"),
    }


def _same_record(result: Mapping[str, Any], decision_id: str) -> None:
    """An answer for another record is never returned as this one."""
    if result.get("record_id") != decision_id:
        raise OperationRefused("UNAVAILABLE", {"reason": "engine result mismatch"})


async def handle_decision_provenance(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """What one decision rested on, with the facts at the revision it used."""
    decision_id = params["decision_id"]
    raw = await _log(context, {"op": "provenance", "record_id": decision_id})
    _same_record(raw, decision_id)
    try:
        return {
            "decision_id": decision_id,
            "record_digest": raw["record_digest"],
            "premises": [
                {"kind": p["kind"], "ref": p["reference"], "class": p["class"]}
                for p in raw["premises"]
            ],
            "facts": [
                {
                    "node_id": f["node_id"],
                    "revision": f["revision"],
                    "digest": f["digest"],
                }
                for f in raw["facts"]
            ],
            "rules": [
                {
                    "rule_id": r["rule_id"],
                    "version": r["version"],
                    "digest": r["digest"],
                }
                for r in raw["rules"]
            ],
            "approver": raw.get("approver"),
            "inputs_state": raw["inputs"],
        }
    except (KeyError, TypeError):
        raise unavailable("engine result malformed") from None


async def handle_decision_why_not(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Why one option was not chosen, re-solved within a bounded budget."""
    decision_id = params["decision_id"]
    action: dict[str, Any] = {
        "op": "why_not",
        "record_id": decision_id,
        "option_id": params["option_id"],
    }
    if params.get("budget_ms") is not None:
        action["budget_ms"] = min(int(params["budget_ms"]), _BUDGET_MAX_MS)
    raw = await _log(context, action)
    _same_record(raw, decision_id)
    try:
        return {
            "decision_id": decision_id,
            "option_id": raw["option_id"],
            "verdict": raw["verdict"],
            "reasons": [
                {"kind": r["kind"], "detail": r["detail"]} for r in raw["reasons"]
            ],
            "budget_used_ms": raw["budget_used_ms"],
            "budget_exhausted": raw["budget_exhausted"],
        }
    except (KeyError, TypeError):
        raise unavailable("engine result malformed") from None


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="decision.list",
            summary="Page the tenant's committed decisions, newest first",
            examples=("list the recent decisions",),
            params=DecisionListParams,
            result=DecisionListResult,
            handler="arbora.api.ops.decision_evidence.handle_decision_list",
            scope=DECISION_READ_SCOPE,
        ),
        build_read_op(
            op_id="decision.provenance.get",
            summary="Read what one decision rested on, at the revision it used",
            examples=("show the provenance of this decision",),
            params=DecisionRefParams,
            result=DecisionProvenanceResult,
            handler="arbora.api.ops.decision_evidence.handle_decision_provenance",
            scope=DECISION_READ_SCOPE,
        ),
        build_read_op(
            op_id="decision.why_not",
            summary="Explain why an option was not chosen, within a bounded budget",
            examples=("why was this option not chosen",),
            params=DecisionWhyNotParams,
            result=DecisionWhyNotResult,
            handler="arbora.api.ops.decision_evidence.handle_decision_why_not",
            scope=DECISION_READ_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "DecisionListParams",
    "DecisionRefParams",
    "DecisionWhyNotParams",
    "handle_decision_list",
    "handle_decision_provenance",
    "handle_decision_why_not",
    "operations",
    "specs",
]
