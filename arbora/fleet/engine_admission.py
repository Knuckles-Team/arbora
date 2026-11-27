"""The engine side of skill and workflow admission (ARBORA-FLEET-R034.9, R034.13, R034.14).

Arbora holds no scoring and computes no compatibility. It asks the engine two
questions and applies the answers:

- ``DecisionEval`` ``certification`` reads the stored ``CertifyLocalSkill``
  attestation by digest (the producer is ``MEDULLA-DECISION-ENGINE-R142.101``; the read
  is ``MEDULLA-DECISION-ENGINE-R142.103``, ``{tenant_id, digest}``, null for an absent or
  foreign digest). It refuses ``UNAVAILABLE`` until the pinned contract declares it;
- ``Decide`` with ``QuestionKind::SkillUpgrade`` returns the contract
  compatibility verdict (``MEDULLA-DECISION-ENGINE-R167.3``).

Both shapes are engine ``main`` shapes behind :mod:`arbora.pin_lag.medulla`
(PIN-LAG: remove the guard calls with the re-pin that carries them).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from arbora.api.domain.fleet.admission_models import AdmissionRefused, canonical_digest
from arbora.api.invoke.pipeline import OperationRefused
from arbora.fleet.definitions import CompatibilityDecision
from arbora.pin_lag import medulla as pin_lag

logger = logging.getLogger(__name__)

__all__ = [
    "EngineAdmission",
    "engine_port",
    "SkillUpgradeQuestion",
    "flatten_attestation",
    "skill_upgrade_param",
]

#: ``send(method, params, tenant)`` returns the decoded engine answer.
Send = Callable[[str, dict[str, Any], str], Awaitable[Any]]
#: The one candidate of an engine ``skill_upgrade`` question.
UPGRADE_OPTION = "admit_upgrade"
_UPGRADE_CODES = frozenset(
    {"SKILL_UPGRADE_BREAKING_WITHOUT_MIGRATION", "SKILL_UPGRADE_DIGEST_MISMATCH"}
)


@dataclass(frozen=True)
class SkillUpgradeQuestion:
    """The previous and new consumer contracts and the declared migration."""

    subject_id: str
    previous_fields: tuple[Mapping[str, Any], ...]
    new_fields: tuple[Mapping[str, Any], ...]
    subject_kind: str = "skill"
    declared_migration_id: str | None = None
    claimed_previous_digest: str | None = None
    claimed_new_digest: str | None = None
    schema_version: int = 1


def skill_upgrade_param(question: SkillUpgradeQuestion) -> dict[str, Any]:
    """The typed ``skill_upgrade`` ``Decide`` parameter (an engine ``SkillUpgradeRequest``)."""
    body: dict[str, Any] = {
        "subject_id": question.subject_id,
        "subject_kind": question.subject_kind,
        "schema_version": question.schema_version,
        "previous": {"fields": [dict(f) for f in question.previous_fields]},
        "new": {"fields": [dict(f) for f in question.new_fields]},
    }
    for name in (
        "declared_migration_id",
        "claimed_previous_digest",
        "claimed_new_digest",
    ):
        value = getattr(question, name)
        if value is not None:
            body[name] = value
    return body


def flatten_attestation(attestation: Mapping[str, Any]) -> dict[str, Any]:
    """The comparable view of an engine ``LocalCertificationAttestation``.

    The skill's component id and definition digest, the model and criteria pins
    and the dependency definition digests are what the binding must equal.
    """
    body = attestation["body"]
    pins = body["pins"]
    return {
        "skill_id": body["skill"]["component_id"],
        "skill_version": body["skill"]["definition_digest"],
        "model_endpoint_revision": pins["model"]["definition_digest"],
        "system_prompt_digest": pins["criteria_policy"]["definition_digest"],
        "dependency_digests": tuple(
            d["definition_digest"] for d in body.get("dependencies", ())
        ),
        "attestation_digest": attestation["digest"],
    }


def _mappings(value: Any) -> Iterator[Mapping[str, Any]]:
    """Every mapping nested in ``value``, outermost first."""
    pending = [value]
    while pending:
        item = pending.pop(0)
        if isinstance(item, Mapping):
            yield item
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)


def _family(value: Any, name: str) -> Any:
    """The typed ``value`` of a ``{"type": name, "value": ...}`` family answer."""
    return next(
        (
            m["value"]
            for m in _mappings(value)
            if m.get("type") == name and "value" in m
        ),
        None,
    )


def _find(value: Any, key: str) -> Any:
    """The first non-null ``key`` of any mapping nested in ``value``."""
    return next((m[key] for m in _mappings(value) if m.get(key) is not None), None)


def _abstained_code(answer: Any) -> str | None:
    """The upgrade refusal code of an abstained record, else ``None``.

    The engine answers a refusal as a record whose outcome is ``abstained`` with a
    ``{"reason": "family", "family": "skill_upgrade", "code": ...}`` reason, not as
    a transport error.
    """
    for item in _mappings(answer):
        if item.get("outcome") != "abstained":
            continue
        for reason in item.get("reasons") or ():
            if isinstance(reason, Mapping) and reason.get("code") in _UPGRADE_CODES:
                return str(reason["code"])
        return "PRODUCER_UNAVAILABLE"
    return None


@dataclass(frozen=True)
class EngineAdmission:
    """The admission gate's engine port over one bound ``send``."""

    send: Send
    feature_schema: Callable[[str], Mapping[str, Any]]

    async def attestation(self, tenant: str, digest: str) -> Mapping[str, Any] | None:
        """The stored attestation of ``digest`` flattened for comparison, or ``None``."""
        try:
            pin_lag.guard_certification_read()
        except OperationRefused as refused:
            raise AdmissionRefused("PRODUCER_UNAVAILABLE", refused.details) from None
        try:
            stored = await self.send(
                "DecisionEval",
                {
                    "op": {
                        "op": "certification",
                        "request": {"tenant_id": tenant, "digest": digest},
                    }
                },
                tenant,
            )
        except Exception:
            logger.debug("attestation failed", exc_info=True)
            return None
        if not isinstance(stored, Mapping) or "body" not in stored:
            return None
        try:
            return flatten_attestation(stored)
        except (KeyError, TypeError):
            return None

    async def skill_upgrade(
        self, tenant: str, question: SkillUpgradeQuestion
    ) -> CompatibilityDecision:
        """Ask the engine whether the contract change is admitted; apply its answer."""
        try:
            pin_lag.guard_skill_upgrade()
        except OperationRefused as refused:
            raise AdmissionRefused("PRODUCER_UNAVAILABLE", refused.details) from None
        from medulla.decision_client import (
            DeclaredOption,
            decide_request,
            declared,
            param,
        )

        # The engine family declares exactly one option, ``admit_upgrade``.
        option = UPGRADE_OPTION
        request = decide_request(
            tenant,
            (f"skill-upgrade:{question.subject_id}", "skill_upgrade", "ordinary"),
            declared([DeclaredOption(option)]),
            self.feature_schema(tenant),
            params=[
                param("skill_upgrade", "skill_upgrade", skill_upgrade_param(question))
            ],
        )
        from arbora.api.domain.deployment.placement import send_decide

        try:
            answer = await send_decide(self.send, request, tenant)
        except Exception as error:
            logger.warning("skill_upgrade failed", exc_info=True)
            code = str(getattr(error, "code", ""))
            if code in _UPGRADE_CODES:
                raise AdmissionRefused(code, {"subject": question.subject_id}) from None
            raise AdmissionRefused("PRODUCER_UNAVAILABLE") from None
        return _decision_of(answer, request, question.subject_id)


def _decision_of(
    answer: Any, request: Mapping[str, Any], subject: str
) -> CompatibilityDecision:
    """Apply the engine's answer: a refusal record raises, an admit record decides."""
    refusal = _abstained_code(answer)
    if refusal is not None:
        raise AdmissionRefused(refusal, {"subject": subject})
    receipt = _family(answer, "skill_upgrade")
    if not isinstance(receipt, Mapping):
        raise AdmissionRefused("PRODUCER_UNAVAILABLE", {"reason": "no admit answer"})
    migration = receipt.get("declared_migration_id") or None
    decision_id = str(_find(answer, "record_id") or canonical_digest(request)[:32])
    return CompatibilityDecision(
        decision_id=decision_id,
        verdict="breaking" if migration else "compatible",
        migration_id=migration,
        readmitted=bool(migration),
    )


def engine_port(send: Send | None, bindings: Any = None) -> EngineAdmission | None:
    """The gate's engine port over the host's bound ``send``; ``None`` when unbound."""
    if send is None:
        return None
    from arbora.api.task_plan_schema import published_pin

    return EngineAdmission(
        send,
        lambda tenant: published_pin(
            (bindings or {}).get("task_plan_schema_pins"), tenant
        ),
    )
