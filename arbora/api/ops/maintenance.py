"""Paper-shortfall maintenance (ARBORA-OPS-R072).

``maintenance.paper_shortfall.run`` purges the papers the engine's ingest-shortfall
verdict allows, under an owner authorization. The admission verdict is the sealed
``IngestShortfall`` decision record (read with ``DecisionLog`` get); it names the
allowed ``purge:<id>`` options (``PurgeCandidate.item_id``) and the funded ``goal:<id>``
options whose declared cost is the order's cost.
Arbora refuses before any effect: a missing authorization is ``SCOPE_REQUIRED``, a
purge the verdict did not allow is ``POLICY_DENIED`` (``purge_not_admitted``) and a
total cost above the ceiling is ``POLICY_DENIED`` (``cost_over_ceiling``).

The engine holds no per-paper lease or authority record, so there is no maintenance
apply method (MEDULLA w16 DECIDED 11). The purge is the one governed delete path: one
``ApplyChangeEnvelopeDraft`` removing the admitted nodes, guarded by
``expected_graph_version``. A stale revision is the engine's ``GRAPH_REVISION_STALE``
and passes through; the engine's own leased, authoritative and last-copy guards apply
to the write. The destructive approval is the one plan confirmation of the operation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.ops._decide import committed_record
from arbora.api.ops.engine_calls import engine_send
from arbora.api.ops.pin_guards import refusal
from arbora.api.ops.question_round import advisory_scores
from arbora.api.registry import (
    AuditClass,
    Composite,
    Confirm,
    Effect,
    Executor,
    Idempotency,
    OpSpec,
    PrincipalRule,
    SubjectRef,
    SubjectSource,
    Verb,
)
from arbora.pin_lag.medulla import PinLagUnavailable, require_methods

SCOPE = "agent:decision-write"
#: The one governed graph write the purge is (a change envelope with a revision guard).
METHOD = "ApplyChangeEnvelopeDraft"
VERDICT_KIND = "ingest_shortfall"


class PaperShortfallRunParams(_common.Params):
    owner_authorization_ref: str | None = Field(default=None, max_length=256)
    #: The graph revision the purge is guarded by (``expected_graph_version``).
    expected_revision: int = Field(ge=0)
    #: Integer cents, 0 to 1,000,000.
    cost_ceiling: int = Field(ge=0, le=1_000_000)
    #: The record id of the sealed ``IngestShortfall`` decision.
    admission_verdict_ref: str = Field(min_length=1, max_length=256)
    #: The ``purge:<id>`` options to apply; each must be one the verdict allows.
    purge_ids: list[str] = Field(min_length=1, max_length=64)


class PaperShortfallRunResult(_common.Params):
    value: dict[str, Any]


def method_published() -> bool:
    try:
        require_methods(METHOD)
    except PinLagUnavailable:
        return False
    return True


def _typed_text(record: Mapping[str, Any], name: str) -> str | None:
    """The text value of one typed question parameter of the sealed record."""
    for item in record.get("params") or ():
        if isinstance(item, Mapping) and item.get("name") == name:
            value = item.get("value")
            return value.get("value") if isinstance(value, Mapping) else None
    return None


def _candidates(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The ``PurgeCandidate`` rows the sealed verdict was asked about (``item_id`` keyed)."""
    text = _typed_text(record, "purge_candidates")
    try:
        rows = json.loads(text) if text else []
        return [r for r in rows if isinstance(r.get("item_id"), str)]
    except (TypeError, ValueError, AttributeError):
        raise OperationRefused(
            "RECEIPT_MISMATCH", {"reason": "verdict_candidates_unreadable"}
        ) from None


def candidate_ids(record: Mapping[str, Any]) -> set[str]:
    """The ``item_id`` of every purge candidate the sealed verdict was asked about."""
    return {str(row["item_id"]) for row in _candidates(record)}


def funded_cost(record: Mapping[str, Any]) -> int:
    """The summed cost in cents of the goals the verdict funded (what the purge makes room for).

    A ``PurgeCandidate`` carries no cost, so the order's cost is the declared cost of each
    ``goal:<id>`` option the engine funded, read from the sealed ``goals`` parameter.
    """
    text = _typed_text(record, "goals")
    funded = {
        name.removeprefix("goal:")
        for name, _ in advisory_scores(record)
        if name.startswith("goal:")
    }
    try:
        rows = json.loads(text) if text else []
        return sum(int(r["cost"]) for r in rows if str(r["goal_id"]) in funded)
    except (TypeError, ValueError, KeyError, AttributeError):
        raise OperationRefused(
            "RECEIPT_MISMATCH", {"reason": "verdict_goals_unreadable"}
        ) from None


def purge_envelope(
    *, node_ids: list[str], expected_revision: int, authorization: str, verdict: str
) -> dict[str, Any]:
    """The change envelope draft that removes ``node_ids`` guarded by the revision."""
    operations = [
        {
            "ordinal": ordinal,
            "surface": "graph",
            "domain": "graph_rows",
            "method": {"method": "RemoveNode", "params": {"node_id": node}},
        }
        for ordinal, node in enumerate(node_ids)
    ]
    digest = hashlib.sha256(
        json.dumps([authorization, verdict, operations], sort_keys=True).encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "envelope_id": f"paper-purge:{digest}",
        "content_version": {
            "object_id": f"paper-purge:{verdict}",
            "digest": digest,
            "digest_algorithm": "sha256",
            "source_version": {"kind": "sequence", "value": expected_revision},
        },
        "privacy": {
            "policy_version": "none",
            "sanitized_payload_digest": digest,
            "sanitizer_version": "none",
        },
        "mutation": {
            "batch_id": f"paper-purge:{digest}",
            "expected_graph_version": expected_revision,
            "operations": operations,
            "outbox": [],
        },
    }


async def _admitted_cost(
    context: Any, params: Mapping[str, Any], wanted: list[str]
) -> int:
    """The summed cost of ``wanted`` under the sealed verdict, or the refusal."""
    record = await committed_record(context, params["admission_verdict_ref"])
    if record.get("question_kind") != VERDICT_KIND:
        raise OperationRefused(
            "RECEIPT_MISMATCH", {"reason": "not_a_shortfall_verdict"}
        )
    allowed = {
        name.removeprefix("purge:")
        for name, _ in advisory_scores(record)
        if name.startswith("purge:")
    }
    if not set(wanted) <= allowed or not set(wanted) <= candidate_ids(record):
        raise OperationRefused("POLICY_DENIED", {"reason": "purge_not_admitted"})
    cost = funded_cost(record)
    if cost > params["cost_ceiling"]:
        raise OperationRefused("POLICY_DENIED", {"reason": "cost_over_ceiling"})
    return cost


async def handle_paper_shortfall_run(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Check the sealed verdict, the authorization and the cost, then purge once."""
    authorization = params.get("owner_authorization_ref")
    if not authorization:
        raise OperationRefused("SCOPE_REQUIRED", {"missing": "owner_authorization_ref"})
    try:
        require_methods(METHOD)
    except PinLagUnavailable as missing:
        raise refusal(missing) from missing
    wanted = list(dict.fromkeys(params["purge_ids"]))
    cost = await _admitted_cost(context, params, wanted)
    receipt = await engine_send(
        context,
        METHOD,
        {
            "draft": purge_envelope(
                node_ids=wanted,
                expected_revision=params["expected_revision"],
                authorization=str(authorization),
                verdict=str(params["admission_verdict_ref"]),
            )
        },
        idempotent=True,
    )
    value = dict(receipt) if isinstance(receipt, Mapping) else {"receipt": receipt}
    return {"value": {**value, "purged": wanted, "cost": cost}}


def operations() -> tuple[OpSpec, ...]:
    if not method_published():
        return ()
    return (
        OpSpec(
            id="maintenance.paper_shortfall.run",
            verb=Verb.ACT,
            summary="Run the paper-shortfall maintenance order under an owner authorization",
            examples=("run the paper shortfall maintenance",),
            params=PaperShortfallRunParams,
            result=PaperShortfallRunResult,
            binding=Composite(
                handler="arbora.api.ops.maintenance.handle_paper_shortfall_run"
            ),
            scopes=frozenset({SCOPE}),
            executor_scopes=frozenset({SCOPE}),
            effect=Effect.DESTRUCTIVE,
            executor=Executor.SERVICE,
            principals=PrincipalRule.SERVICE_ONLY,
            subject=SubjectRef(source=SubjectSource.CALLER_TENANT),
            idempotency=Idempotency.KEY_REQUIRED,
            audit=AuditClass.IDENTITY_CHAIN,
            confirm=Confirm.PLAN,
        ),
    )


specs = operations

#: The engine replays the change envelope by the request key (one purge per key).
NATIVE_IDEMPOTENCY: Mapping[str, str] = MappingProxyType(
    {
        "maintenance.paper_shortfall.run": "medulla:ApplyChangeEnvelopeDraft",
    }
)

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "METHOD",
    "PaperShortfallRunParams",
    "candidate_ids",
    "funded_cost",
    "purge_envelope",
    "handle_paper_shortfall_run",
    "method_published",
    "operations",
    "specs",
]
