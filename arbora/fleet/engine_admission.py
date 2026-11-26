"""The engine side of skill and workflow admission (ARBORA-FLEET-R034.9, R034.13, R034.14).

Arbora holds no scoring and computes no compatibility. It asks the engine two
questions and applies the answers:

- ``DecisionEval`` ``receipt`` reads the stored ``CertifyLocalSkill`` attestation
  by digest (the producer is ``MEDULLA-DECISION-ENGINE-R142.101``);
- ``Decide`` with ``QuestionKind::SkillUpgrade`` returns the contract
  compatibility verdict (``MEDULLA-DECISION-ENGINE-R167.3``).

Both shapes are engine ``main`` shapes behind :mod:`arbora.pin_lag.medulla`
(PIN-LAG: remove the guard calls with the re-pin that carries them).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.fleet.admission_models import AdmissionRefused, canonical_digest
from arbora.fleet.definitions import CompatibilityDecision
from arbora.pin_lag import medulla as pin_lag

__all__ = [
    "EngineAdmission",
    "SkillUpgradeQuestion",
    "flatten_attestation",
    "skill_upgrade_param",
]

#: ``send(method, params, tenant)`` returns the decoded engine answer.
Send = Callable[[str, dict[str, Any], str], Awaitable[Any]]
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


def _family(value: Any, name: str) -> Any:
    """The typed ``value`` of a ``{"type": name, "value": ...}`` family answer."""
    if isinstance(value, Mapping):
        if value.get("type") == name and "value" in value:
            return value["value"]
        for item in value.values():
            found = _family(item, name)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _family(item, name)
            if found is not None:
                return found
    return None


def _find(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        if key in value:
            return value[key]
        for item in value.values():
            found = _find(item, key)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _find(item, key)
            if found is not None:
                return found
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
            answer = await self.send(
                "DecisionEval",
                {
                    "op": "receipt",
                    "request": {"receipt_digest": digest, "tenant_id": tenant},
                },
                tenant,
            )
        except Exception:
            return None
        stored = _find(answer, "attestation")
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

        option = f"skill-upgrade:{question.subject_id}"
        request = decide_request(
            tenant,
            (option, "skill_upgrade", "ordinary"),
            declared([DeclaredOption(option)]),
            self.feature_schema(tenant),
            params=[
                param("skill_upgrade", "skill_upgrade", skill_upgrade_param(question))
            ],
        )
        try:
            answer = await self.send("Decide", request, tenant)
        except Exception as error:
            code = str(getattr(error, "code", ""))
            if code in _UPGRADE_CODES:
                raise AdmissionRefused(code, {"subject": question.subject_id}) from None
            raise AdmissionRefused("PRODUCER_UNAVAILABLE") from None
        receipt = _family(answer, "skill_upgrade")
        if not isinstance(receipt, Mapping):
            raise AdmissionRefused(
                "PRODUCER_UNAVAILABLE", {"reason": "no admit answer"}
            )
        migration = receipt.get("declared_migration_id") or None
        decision_id = str(_find(answer, "record_id") or canonical_digest(request)[:32])
        return CompatibilityDecision(
            decision_id=decision_id,
            verdict="breaking" if migration else "compatible",
            migration_id=migration,
            readmitted=bool(migration),
        )
