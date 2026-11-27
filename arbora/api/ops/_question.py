"""One ``Decide`` question round trip for the operations that only sequence engine answers.

An operation sends a typed question, reads the one sealed record the engine
answers and acts on its chosen option. Arbora scores, ranks and thresholds
nothing here: the engine decides, and its refusal keeps its own code. A question
kind the pinned engine does not list refuses ``UNAVAILABLE`` naming it
(PIN-LAG: remove with the re-pin that carries the kind).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import claim_values
from arbora.api.ops import _engine
from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_question_kind

#: One in the engine's Q32 fixed-point scale; a chosen advisory option scores it.
Q32_ONE = 1 << 32


def question_request(
    tenant: str,
    question_id: str,
    kind: str,
    candidates: Mapping[str, Any],
    feature_schema: Mapping[str, Any],
    params: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """The ``DecideRequest`` of one ordinary question (wire names, Rust field order)."""
    from medulla import decision_client as dc

    return dc.decide_request(
        tenant,
        (question_id, kind, "ordinary"),
        candidates,
        feature_schema,
        params=params,
    )


class FamilyRefusal(Exception):
    """A refusal the engine's question family names (the leading token of its detail)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def _send_decide(
    context: Any, request: Mapping[str, Any], family_codes: frozenset[str]
) -> Any:
    from medulla import EngineResponseError
    from medulla.generated import send_by_id

    _engine._require_method("Decide")
    graph = await _engine.caller_dispatch_graph(context)
    try:
        with _engine.client_scope(
            context.client, claim_values(context.caller.engine_claims)
        ):
            payload = await send_by_id(
                context.client,
                "Decide",
                {"request": dict(request)},
                graph,
                idempotency_key=None,
            )
    except EngineResponseError as exc:
        token = (exc.detail or "").split(":", 1)[0].strip()
        if token in family_codes:
            raise FamilyRefusal(token) from exc
        raise EngineRefusal(_engine.refusal_code(exc.code, exc.detail)) from exc
    return _engine.decode(payload)


async def decide_question(
    context: Any,
    kind: str,
    request: Mapping[str, Any],
    *,
    family_codes: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    """Send ``Decide`` and return its one sealed record.

    A refusal whose detail leads with one of ``family_codes`` is the family's own and
    raises :class:`FamilyRefusal`; every other engine refusal keeps its engine code.
    """
    try:
        require_question_kind(kind)
    except PinLagUnavailable as missing:
        raise refusal(missing) from missing
    answer = await _send_decide(context, request, family_codes)
    records = answer.get("records") if isinstance(answer, Mapping) else None
    if not isinstance(records, list) or len(records) != 1:
        raise OperationRefused(
            "RECEIPT_MISMATCH", {"reason": "expected exactly one decision record"}
        )
    record = records[0]
    if not isinstance(record, Mapping):
        raise OperationRefused("RECEIPT_MISMATCH", {"reason": "malformed record"})
    return record


def advisory_scores(record: Mapping[str, Any]) -> tuple[tuple[str, int], ...]:
    """The ``(option_id, score)`` pairs of an advisory answer, in the engine's order."""
    outcome = record.get("outcome")
    if not isinstance(outcome, Mapping) or outcome.get("outcome") != "advisory":
        raise OperationRefused("RECEIPT_MISMATCH", {"reason": "answer is not advisory"})
    return tuple(
        (str(item["option_id"]), int(item["score"]["value"]))
        for item in outcome.get("scores", ())
    )


def chosen_option(record: Mapping[str, Any]) -> str:
    """The one option an advisory answer scores at 1.0."""
    chosen = [name for name, score in advisory_scores(record) if score == Q32_ONE]
    if len(chosen) != 1:
        raise OperationRefused(
            "RECEIPT_MISMATCH", {"reason": "no single chosen option"}
        )
    return chosen[0]


def q32_permille(permille: int) -> int:
    """``permille`` thousandths on the Q32 scale, as the engine rounds it."""
    return (permille * Q32_ONE + 500) // 1000


__all__ = [
    "FamilyRefusal",
    "Q32_ONE",
    "advisory_scores",
    "chosen_option",
    "decide_question",
    "q32_permille",
    "question_request",
]
