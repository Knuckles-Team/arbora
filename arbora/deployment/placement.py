"""Ask the engine where a deployment component runs (ARBORA-DEPLOY-R017.1).

Arbora adds no planner. One typed ``Decide`` question of kind ``placement`` goes
to the engine; the engine answers with its record, which is checked against the
request and the verified principal and returned unchanged. The candidate
targets come from the caller's request, so no backend is named or ordered here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused

#: The one Decide question this module asks.
PLACEMENT_QUESTION_ID = "deployment.placement"

Decide = Callable[[dict[str, Any]], Awaitable[Any]]


def placement_body(
    subject: Mapping[str, Any],
    constraints: Mapping[str, Any],
    candidate_targets: Sequence[str],
) -> dict[str, Any]:
    """The engine's ``PlacementRequestBody``, checked against its typed contract."""
    from medulla.generated.models import PlacementRequestBody

    body = {
        "subject": dict(subject),
        "constraints": dict(constraints),
        "candidate_targets": list(candidate_targets),
    }
    try:
        PlacementRequestBody.model_validate(body)
    except ValueError as exc:
        raise OperationRefused("INVALID_ARGUMENT", {"message": str(exc)}) from exc
    return body


def single_record(batch: Any) -> Mapping[str, Any]:
    """The one decision record of a batch, or ``RECEIPT_MISMATCH``."""
    records = batch.get("records") if isinstance(batch, Mapping) else None
    if not isinstance(records, list) or len(records) != 1:
        raise OperationRefused(
            "RECEIPT_MISMATCH", {"reason": "expected exactly one decision record"}
        )
    if not isinstance(records[0], Mapping):
        raise OperationRefused("RECEIPT_MISMATCH", {"reason": "malformed record"})
    return records[0]


def check_receipt(
    record: Mapping[str, Any], request: Mapping[str, Any], principal: str
) -> None:
    """The record answers this request, for this tenant, to this principal."""
    expected = (
        "principal:sha256:" + hashlib.sha256(principal.encode("utf-8")).hexdigest()
    )
    inputs = record.get("inputs")
    bound = (
        record.get("caller_principal") == expected
        and record.get("tenant_id") == request["tenant_id"]
        and record.get("question") == request["question"]
        and isinstance(inputs, Mapping)
        and inputs.get("params") == request["params"]
        and bool(record.get("record_id"))
        and bool(record.get("record_digest"))
    )
    if not bound:
        raise OperationRefused(
            "RECEIPT_MISMATCH", {"reason": "the record does not answer this request"}
        )


async def ask_placement(
    decide: Decide,
    *,
    tenant: str,
    principal: str,
    graph: str,
    feature_schema: Mapping[str, Any],
    subject: Mapping[str, Any],
    constraints: Mapping[str, Any],
    candidate_targets: Sequence[str],
    policy: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Send one placement ``Decide`` and return the engine record unchanged."""
    from medulla.decision_client import DeclaredOption, decide_request, declared, param

    body = placement_body(subject, constraints, candidate_targets)
    request = decide_request(
        tenant,
        (PLACEMENT_QUESTION_ID, "placement", "ordinary"),
        declared([DeclaredOption(target) for target in body["candidate_targets"]]),
        feature_schema,
        policy=policy,
        params=[param("placement", "placement", {"body": body, "graph": graph})],
    )
    record = single_record(await decide(request))
    check_receipt(record, request, principal)
    return record


def chosen_target(record: Mapping[str, Any]) -> str:
    """The engine's top-scored option id, as the placement target name."""
    outcome = record.get("outcome")
    scores = outcome.get("scores") if isinstance(outcome, Mapping) else None
    first = scores[0] if isinstance(scores, list) and scores else None
    option = first.get("option_id") if isinstance(first, Mapping) else None
    if not isinstance(option, str) or not option:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the engine chose no placement target"}
        )
    return option.removeprefix("placement:")
