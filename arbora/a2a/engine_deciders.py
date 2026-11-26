"""The engine deciders of the human-grant chain (ARBORA-A2A-R005.4, ARBORA-A2A-R006.4).

Each decider asks the engine one ``Decide`` question through an injected ``ask`` seam and
reads the record's typed ``FamilyAnswer``. The answer models come from
``arbora.pin_lag.medulla`` so a pinned engine without them refuses ``UNAVAILABLE`` naming
the missing shape (PIN-LAG: the guard goes after the engine re-pin). An answer of any
other shape raises, and the admitters read a raise as ``ENGINE_UNAVAILABLE``: a verdict
is never inferred.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from arbora.pin_lag.medulla import admission_models

#: ``(question kind, params, tenant)`` to the engine ``Decide`` record, or ``None``.
AskDecide = Callable[[str, Mapping[str, str], str], Awaitable[Mapping[str, Any] | None]]

TOOL_ACTION_KIND = "tool_action_admission"
HELD_ITEM_KIND = "held_item_admission"


def _text(value: Any) -> str:
    return str(getattr(value, "value", value))


class EngineToolActionDecider:
    """``ToolActionDecider``: the engine's typed tool-action verdict, unchanged."""

    def __init__(self, ask: AskDecide) -> None:
        self._ask = ask

    async def decide(self, request: Any, *, tenant: str) -> Mapping[str, Any] | None:
        models = admission_models()
        record = await self._ask(
            TOOL_ACTION_KIND,
            {
                "pending_call_id": request.pending_call_id,
                "params_digest": request.params_digest,
                "trust_mark_ref": request.trust_mark_ref,
                "action_class": _text(request.action_class),
            },
            tenant,
        )
        if record is None:
            return None
        answer = models.FamilyAnswerToolActionAdmission.model_validate(
            record["answer"]
        ).value
        verdict = _text(answer.verdict)
        if verdict == "abstain":
            return {"verdict": "abstain"}
        return {
            "verdict": verdict,
            "approval_ref": answer.approval_ref,
            "decision_ref": answer.decision_ref,
        }


class EngineHeldItemDecider:
    """``HeldItemDecider``: the engine's typed held-item verdict, unchanged."""

    def __init__(self, ask: AskDecide) -> None:
        self._ask = ask

    async def decide(self, request: Any, *, tenant: str) -> Mapping[str, Any] | None:
        models = admission_models()
        record = await self._ask(
            HELD_ITEM_KIND,
            {
                "item_kind": request.item_kind,
                "item_ref": request.item_ref,
                "content_digest": request.content_digest,
                "engine_decision_ref": request.engine_decision_ref,
            },
            tenant,
        )
        if record is None:
            return None
        answer = models.FamilyAnswerHeldItemAdmission.model_validate(
            record["answer"]
        ).value
        found = {
            "outcome": _text(answer.outcome),
            "verdict_ref": answer.verdict_ref,
            "verification_ref": answer.verification_ref,
            "verifier": answer.verifier,
            "compiler": answer.compiler,
            "reason": answer.reason,
        }
        return {name: value for name, value in found.items() if value is not None}
