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
    """``record.answer`` is ``FamilyAnswer::HarnessConfig``: ``{type, value}``.

    ``value`` is the engine's ``HarnessConfigEvaluationEvidence``; its ``answer`` is the
    ``HarnessConfigAnswer`` (medulla ``StatisticalDecisionRecord.answer``).
    """
    family = record.get("answer")
    if not isinstance(family, Mapping) or family.get("type") != QUESTION_KIND:
        raise ValueError("the record carries no harness configuration")
    value = family.get("value")
    answer = value.get("answer") if isinstance(value, Mapping) else None
    if not isinstance(answer, Mapping):
        raise ValueError("the record carries no harness configuration")
    return answer


def _candidates(model_profile_ref: str, tool_refs: tuple[str, ...]) -> Any:
    """The model profile and the routed agent's library tools, declared as candidates."""
    from medulla.decision_client import DeclaredOption, declared

    return declared(
        [DeclaredOption(model_profile_ref), *map(DeclaredOption, tool_refs)]
    )


def _expires_at_ms(record: Mapping[str, Any]) -> int | None:
    """The expiry the engine seals on ``HarnessConfigEvaluationEvidence``.

    MEDULLA w14 (R128.17) seals ``expires_at_ms`` on the evidence when the request
    carried ``valid_for_ms``. PIN-LAG: the pinned release seals none, so the value
    reads ``None`` until the re-pin (no edit needed then). A present value that is not a non-negative integer is a malformed
    record: it raises, so the admission refuses ``HARNESS_CONFIG_UNAVAILABLE``.
    Arbora holds no default lifetime.
    """
    family = record.get("answer")
    value = family.get("value") if isinstance(family, Mapping) else None
    raw = value.get("expires_at_ms") if isinstance(value, Mapping) else None
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError("the record carries a malformed expiry")
    return raw


def _params(
    model_profile_ref: str,
    task_class: str,
    budget_tokens: int,
    priority: str,
    valid_for_ms: int | None,
) -> list[Any]:
    """The typed params of the question; ``valid_for_ms`` only when one was asked."""
    from medulla.decision_client import param

    found = [
        param("model_profile_ref", "iri", model_profile_ref),
        param("task_class", "iri", task_class),
        param("usable_context_budget_tokens", "int", budget_tokens),
        param("priority", "text", priority),
    ]
    if valid_for_ms:  # MEDULLA-DECISION-ENGINE-R128.17: the engine seals the expiry
        found.append(param("valid_for_ms", "int", valid_for_ms))
    return found


class EngineHarnessConfigDecider:
    """Ask the engine for the harness configuration of one admitted task."""

    def __init__(
        self,
        decide: Decide,
        *,
        feature_schema: Callable[[str], Mapping[str, Any]],
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
        tool_refs: tuple[str, ...] = (),
        valid_for_ms: int | None = None,
    ) -> HarnessConfigRecord:
        from medulla.decision_client import decide_request

        from arbora.api.domain.deployment.placement import check_receipt, single_record

        request = decide_request(
            tenant,
            (f"a2a.harness:{task_class}", QUESTION_KIND, "ordinary"),
            _candidates(model_profile_ref, tool_refs),
            self._feature_schema(tenant),
            params=_params(
                model_profile_ref, task_class, budget_tokens, priority, valid_for_ms
            ),
        )
        record = single_record(
            await asyncio.wait_for(self._decide(request), self._timeout_s)
        )
        check_receipt(record, request, self._principal())
        digest = _HEX.search(str(record.get("record_digest", "")))
        if digest is None:
            raise ValueError("the record carries no digest")
        return HarnessConfigRecord(
            answer=HarnessConfigMirror.model_validate(_answer(record)),
            decision_record_ref=str(record["record_id"]),
            digest=digest.group(0),
            expires_at_ms=_expires_at_ms(record),
        )


def _configured_timeout() -> Any:
    from arbora.core.environment import read_env

    return read_env("A2A_ENGINE_DECISION_TIMEOUT_S")
