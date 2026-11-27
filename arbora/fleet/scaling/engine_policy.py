"""The scale verdict is the engine's decision (ARBORA-FLEET-R024.16).

Scaling is a decision, and the engine owns decisions. One typed ``Decide`` question of
kind ``scale_intent`` asks the engine whether a persisted intent may actuate, and the
engine answers ``allow`` or ``refuse:<CODE>`` as the first score of its advisory
outcome. Arbora decides nothing here: it sends the intent as the verified caller of the
running reconcile, checks that the sealed record answers exactly this request, and
reports the verdict. An ``allow`` is the only way the actuator is reached; a refusal is
a denied result that only a break-glass authorization passes. The question kind is
engine ``main``, so a pinned release that does not declare it refuses ``UNAVAILABLE``.
"""

from __future__ import annotations

import json
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._question import advisory_scores
from arbora.fleet.scaling.fleet_actuator import request_context
from arbora.fleet.scaling.scaling_intent_reconciler import (
    ScaleIntentRecord,
    ScalePolicyDecision,
    _derived_id,
)
from arbora.pin_lag.medulla import refusal, require_question_kind
from arbora.pin_lag.unavailable import PinLagUnavailable

#: The engine question kind of a scale verdict.
QUESTION_KIND = "scale_intent"
ALLOW = "allow"
REFUSE_PREFIX = "refuse:"
MAX_REASON = 256


class EngineScalePolicy:
    """``ScalePolicy`` whose verdict is the engine's ``Decide`` answer."""

    async def decide(self, intent: ScaleIntentRecord) -> ScalePolicyDecision:
        try:
            require_question_kind(QUESTION_KIND)
        except PinLagUnavailable as missing:
            raise refusal(missing) from missing
        from medulla.decision_client import (
            DeclaredOption,
            decide_request,
            declared,
            param,
        )

        from arbora.api.invoke.bootstrap import caller_dispatch_graph
        from arbora.api.ops._engine import engine_send
        from arbora.api.ops.deploy import feature_schema_of
        from arbora.deployment.placement import (
            check_receipt,
            send_decide,
            single_record,
        )

        context = request_context()
        tenant = str(context.caller.tenant)
        request = decide_request(
            tenant,
            (f"fleet.scale:{intent.execution_key}", QUESTION_KIND, "ordinary"),
            declared([DeclaredOption("verdict")]),
            feature_schema_of(context, tenant),
            params=[
                param("graph", "text", await caller_dispatch_graph(context)),
                param(
                    "intent",
                    "text",
                    json.dumps(intent.model_dump(mode="json"), sort_keys=True),
                ),
            ],
        )

        async def send(method: str, params: dict[str, Any], *_tail: Any) -> Any:
            return await engine_send(context, method, params)

        record = single_record(await send_decide(send, request, tenant))
        check_receipt(record, request, str(context.caller.principal))
        return _verdict(record, intent)


def _verdict(record: Any, intent: ScaleIntentRecord) -> ScalePolicyDecision:
    """The engine's allow or refusal as the durable decision, keyed by the sealed record."""
    scores = advisory_scores(record)
    option = scores[0][0] if scores else ""
    if option != ALLOW and not option.startswith(REFUSE_PREFIX):
        raise OperationRefused(
            "RECEIPT_MISMATCH", {"reason": "the record carries no scale verdict"}
        )
    return ScalePolicyDecision(
        decision_id=_derived_id("decision", str(record["record_id"])),
        allowed=option == ALLOW,
        reason=(
            "allowed by the engine"
            if option == ALLOW
            else f"refused by the engine: {option.removeprefix(REFUSE_PREFIX)}"[
                :MAX_REASON
            ]
        ),
    )


__all__ = ["QUESTION_KIND", "EngineScalePolicy"]
