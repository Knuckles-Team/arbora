"""The engine adapter of the harness-configuration decision port (ARBORA-A2A-R017.1).

``EngineHarnessConfigDecider`` sends one ``harness_config`` ``Decide`` question
(``MEDULLA-DECISION-ENGINE-R128.2``) as the verified caller and reads the committed
record back as a ``HarnessConfigRecord``. Arbora ranks and defaults nothing: the
record's answer, its id and its digest are the engine's.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from arbora.messaging.webhook_delivery import clamp_setting

from .admission import HarnessConfigMirror, HarnessConfigRecord, Priority

__all__ = ["DECISION_TIMEOUT_S", "EngineHarnessConfigDecider"]

QUESTION_KIND = "harness_config"
#: ``A2A_ENGINE_DECISION_TIMEOUT_S``: ``(default, (low, high))`` seconds.
DECISION_TIMEOUT_S = (10, (1, 60))
_HEX = re.compile(r"[0-9a-f]{64}")

Decide = Callable[[dict[str, Any]], Awaitable[Any]]


def _answer(record: Mapping[str, Any]) -> Mapping[str, Any]:
    outcome = record.get("outcome")
    family = outcome.get("family_answer") if isinstance(outcome, Mapping) else None
    value = family.get("value") if isinstance(family, Mapping) else None
    answer = value.get("answer") if isinstance(value, Mapping) else None
    if not isinstance(answer, Mapping):
        raise ValueError("the record carries no harness configuration")
    return answer


class EngineHarnessConfigDecider:
    """Ask the engine for the harness configuration of one admitted task."""

    def __init__(
        self,
        decide: Decide,
        *,
        feature_schema: Mapping[str, Any],
        principal: Callable[[], str],
        timeout_s: float | None = None,
    ) -> None:
        self._decide = decide
        self._feature_schema = feature_schema
        self._principal = principal
        self._timeout_s = float(
            clamp_setting(
                timeout_s if timeout_s is not None else _configured_timeout(),
                DECISION_TIMEOUT_S,
            )
        )

    async def decide(
        self,
        *,
        tenant: str,
        model_profile_ref: str,
        task_class: str,
        budget_tokens: int,
        priority: Priority,
    ) -> HarnessConfigRecord:
        from medulla.decision_client import (
            DeclaredOption,
            decide_request,
            declared,
            param,
        )

        from arbora.deployment.placement import check_receipt, single_record

        request = decide_request(
            tenant,
            (f"a2a.harness:{task_class}", QUESTION_KIND, "ordinary"),
            declared([DeclaredOption(model_profile_ref)]),
            self._feature_schema,
            params=[
                param("model_profile_ref", "iri", model_profile_ref),
                param("task_class", "iri", task_class),
                param("usable_context_budget_tokens", "int", budget_tokens),
                param("priority", "text", priority),
            ],
        )
        record = single_record(
            await asyncio.wait_for(self._decide(request), self._timeout_s)
        )
        check_receipt(record, request, self._principal())
        digest = _HEX.search(str(record.get("record_digest", "")))
        if digest is None:
            raise ValueError("the record carries no digest")
        expires = record.get("expires_at_ms")
        return HarnessConfigRecord(
            answer=HarnessConfigMirror.model_validate(_answer(record)),
            decision_record_ref=str(record["record_id"]),
            digest=digest.group(0),
            expires_at_ms=expires if isinstance(expires, int) else None,
        )


def _configured_timeout() -> Any:
    from arbora.core.settings import setting

    return setting("A2A_ENGINE_DECISION_TIMEOUT_S", "10")
