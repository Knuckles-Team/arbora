"""The scale verdict is the engine's decision (ARBORA-FLEET-R024.16).

Scaling is a decision, and the engine owns decisions. One typed ``Decide`` question of
kind ``scale_intent`` asks the engine whether a persisted intent may actuate, and the
engine answers ``allow`` or ``refuse:<CODE>`` as the first score of its advisory
outcome. Arbora decides nothing here: it sends the intent as the verified caller of the
running reconcile, checks that the sealed record answers exactly this request, and
reports the verdict. An ``allow`` is the only way the actuator is reached; a refusal is
a denied result that only a break-glass authorization passes. The question kind is
engine ``main``: the request is built by ``medulla.decision_client.scale_intent_request``
(MEDULLA-DECISION-ENGINE-R833), so a pinned release without that builder refuses
``UNAVAILABLE`` naming ``decision_client.scale_intent_request``.
"""

from __future__ import annotations

from typing import Any

from arbora.api.domain.fleet.scale_records import (
    ScaleIntentRecord,
    ScalePolicyDecision,
    derived_id,
)
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.request_binding import request_context
from arbora.api.ops.pin_guards import refusal
from arbora.api.ops.question_round import advisory_scores
from arbora.pin_lag.medulla import require_symbol
from arbora.pin_lag.unavailable import PinLagUnavailable

#: The engine question kind of a scale verdict and the builder that frames it.
QUESTION_KIND = "scale_intent"
REQUEST_BUILDER = ("medulla.decision_client", "scale_intent_request")
#: The intent fields the engine reads (``ScaleIntentRecord``); the rest never leave arbora.
WIRE_FIELDS = frozenset(
    {
        "intent_id",
        "intent_revision",
        "expected_unit_revision",
        "desired_replicas",
        "min_replicas",
        "max_replicas",
        "controller_mode",
        "replica_writer_id",
        "execution_key",
        "fence",
        "target",
    }
)
ALLOW = "allow"
REFUSE_PREFIX = "refuse:"
MAX_REASON = 256


class EngineScalePolicy:
    """``ScalePolicy`` whose verdict is the engine's ``Decide`` answer."""

    async def decide(self, intent: ScaleIntentRecord) -> ScalePolicyDecision:
        try:
            require_symbol(*REQUEST_BUILDER)
        except PinLagUnavailable as missing:
            raise refusal(missing) from missing
        from medulla.decision_client import scale_intent_request

        from arbora.api.domain.deployment.placement import (
            check_receipt,
            send_decide,
            single_record,
        )
        from arbora.api.invoke.bootstrap import caller_dispatch_graph
        from arbora.api.ops.deploy import feature_schema_of
        from arbora.api.ops.engine_calls import engine_send

        context = request_context()
        tenant = str(context.caller.tenant)
        request = scale_intent_request(
            tenant,
            f"fleet.scale:{intent.execution_key}",
            await caller_dispatch_graph(context),
            _wire_intent(intent),
            feature_schema_of(context, tenant),
        )

        async def send(method: str, params: dict[str, Any], *_tail: Any) -> Any:
            return await engine_send(context, method, params)

        record = single_record(await send_decide(send, request, tenant))
        check_receipt(record, request, str(context.caller.principal))
        return _verdict(record, intent)


def _wire_intent(intent: ScaleIntentRecord) -> dict[str, Any]:
    """The intent as the engine's ``ScaleIntentRecord``: only the fields it reads."""
    body = intent.model_dump(mode="json")
    return {name: body[name] for name in sorted(WIRE_FIELDS)}


def _verdict(record: Any, intent: ScaleIntentRecord) -> ScalePolicyDecision:
    """The engine's allow or refusal as the durable decision, keyed by the sealed record."""
    scores = advisory_scores(record)
    option = scores[0][0] if scores else ""
    if option != ALLOW and not option.startswith(REFUSE_PREFIX):
        raise OperationRefused(
            "RECEIPT_MISMATCH", {"reason": "the record carries no scale verdict"}
        )
    return ScalePolicyDecision(
        decision_id=derived_id("decision", str(record["record_id"])),
        allowed=option == ALLOW,
        reason=(
            "allowed by the engine"
            if option == ALLOW
            else f"refused by the engine: {option.removeprefix(REFUSE_PREFIX)}"[
                :MAX_REASON
            ]
        ),
    )


__all__ = ["QUESTION_KIND", "REQUEST_BUILDER", "WIRE_FIELDS", "EngineScalePolicy"]
