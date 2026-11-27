"""The engine ``credential_reuse`` decider and the ``Decide`` seam every decider asks through."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

__all__ = ["CREDENTIAL_REUSE_KIND", "AskDecide", "EngineCredentialReuseDecider"]

#: ``(question kind, params, tenant)`` to the engine ``Decide`` record, or ``None``.
AskDecide = Callable[[str, Mapping[str, str], str], Awaitable[Mapping[str, Any] | None]]

#: ``credential_reuse`` (ARBORA-IDENTITY-R023.3): admit, deny or abstain with a reason.
CREDENTIAL_REUSE_KIND = "credential_reuse"


def _text(value: Any) -> str:
    return str(getattr(value, "value", value))


class EngineCredentialReuseDecider:
    """The engine's typed ``credential_reuse`` verdict: admit, deny or abstain.

    The family answer model is PIN-LAG: a pinned engine without it refuses ``UNAVAILABLE``
    naming ``FamilyAnswerCredentialReuse`` and nothing is inferred.
    """

    def __init__(self, ask: AskDecide) -> None:
        self._ask = ask

    async def decide(self, request: Any, *, tenant: str) -> Mapping[str, Any] | None:
        from arbora.pin_lag.medulla import require_models

        require_models(("FamilyAnswerCredentialReuse",))
        from medulla.generated import models

        record = await self._ask(
            CREDENTIAL_REUSE_KIND,
            {
                "owner": request.owner_principal,
                "agent_id": request.client,
                "connector": request.connector,
                "provider": request.provider,
                "scopes": " ".join(request.scopes),
                "purpose": request.operation,
            },
            tenant,
        )
        if record is None:
            return None
        answer = models.FamilyAnswerCredentialReuse.model_validate(
            record["answer"]
        ).value
        return {
            "verdict": _text(answer.verdict),
            "reason": getattr(answer, "reason", None),
            "decision_ref": getattr(answer, "decision_ref", None)
            or str(record.get("id", "")),
        }
