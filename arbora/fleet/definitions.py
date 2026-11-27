"""Versioned fleet definitions and their admission (ARBORA-FLEET-R002.3 to R002.6).

A tool, prompt or skill definition carries a semantic version and digests.
Arbora applies the compatibility decision that medulla returns from ``Decide``
with ``QuestionKind::SkillUpgrade``; it computes no compatibility itself. A
publication swaps the catalog generation atomically, and a refusal leaves the
prior generation active.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from scion_sdk.manifest.fingerprints import output_schema_digest

from arbora.api.domain.fleet.admission_models import Digest, canonical_digest

__all__ = [
    "REFUSALS",
    "CompatibilityDecision",
    "DefinitionAdmissionReceipt",
    "DefinitionGeneration",
    "DefinitionRefused",
    "VersionedDefinition",
    "admit_definition",
    "parse_definition",
]

REFUSALS: dict[str, str] = {
    "DEFINITION_VERSION_REQUIRED": "the definition declares no semantic version",
    "SCHEMA_INVALID": "the definition has an unknown or malformed field",
    "SCHEMA_INCOMPATIBLE": "the engine did not admit the change as compatible",
    "DEFINITION_VERSION_CONFLICT": "the version is not newer, or changed under an equal version",
    "PRODUCER_UNAVAILABLE": "no compatibility decision was produced",
}

_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class DefinitionRefused(Exception):
    """A refusal from :data:`REFUSALS`, raised before the item is visible in ``find``."""

    def __init__(self, code: str, detail: Mapping[str, Any] | None = None) -> None:
        assert code in REFUSALS
        self.code = code
        self.detail = dict(detail or {})
        super().__init__(f"{code}: {self.detail}")


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VersionedDefinition(_Closed):
    kind: Literal["tool", "prompt", "skill"]
    producer_id: str = Field(min_length=1)
    version: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    schema_digest: Digest
    body_digest: Digest
    compatibility_receipt_ref: str | None = None

    @property
    def output_digest(self) -> str:
        """A missing output schema takes the SDK's canonical nonempty digest."""
        tool = (
            {} if self.output_schema is None else {"outputSchema": self.output_schema}
        )
        return output_schema_digest(tool)

    @property
    def semver(self) -> tuple[int, int, int]:
        # ``validate_definition`` refuses any version that is not semver.
        match = cast("re.Match[str]", _SEMVER.fullmatch(self.version))
        major, minor, patch = match.groups()
        return int(major), int(minor), int(patch)


def parse_definition(data: Mapping[str, Any]) -> VersionedDefinition:
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise DefinitionRefused("DEFINITION_VERSION_REQUIRED")
    try:
        definition = VersionedDefinition.model_validate(dict(data))
    except ValidationError as error:
        raise DefinitionRefused(
            "SCHEMA_INVALID",
            {"fields": sorted({".".join(map(str, e["loc"])) for e in error.errors()})},
        ) from None
    if _SEMVER.fullmatch(definition.version) is None:
        raise DefinitionRefused("SCHEMA_INVALID", {"fields": ["version"]})
    return definition


@dataclass(frozen=True)
class CompatibilityDecision:
    """The engine's ``SkillUpgrade`` answer; arbora only applies it."""

    decision_id: str
    verdict: Literal["compatible", "breaking"]
    migration_id: str | None = None
    readmitted: bool = False


class DefinitionAdmissionReceipt(_Closed):
    """Written by the pack import step into the caller's tenant; holds no bodies."""

    tenant: str
    definition_key: str
    old_version: str | None
    new_version: str
    old_schema_digest: str | None
    new_schema_digest: str
    producer_id: str
    decision_id: str | None
    generation: int


def admit_definition(
    prior: VersionedDefinition | None,
    candidate: VersionedDefinition,
    decision: CompatibilityDecision | None,
    *,
    tenant: str = "",
    generation: int = 0,
) -> DefinitionAdmissionReceipt:
    """Admit ``candidate`` over ``prior`` by the engine ``decision``, or refuse."""

    def receipt(decision_id: str | None) -> DefinitionAdmissionReceipt:
        return DefinitionAdmissionReceipt(
            tenant=tenant,
            definition_key=f"{candidate.kind}:{candidate.producer_id}",
            old_version=prior.version if prior else None,
            new_version=candidate.version,
            old_schema_digest=prior.schema_digest if prior else None,
            new_schema_digest=candidate.schema_digest,
            producer_id=candidate.producer_id,
            decision_id=decision_id,
            generation=generation,
        )

    if prior is None:
        return receipt(decision.decision_id if decision else None)
    old, new = prior.semver, candidate.semver
    if new == old:
        if (prior.schema_digest, prior.body_digest) != (
            candidate.schema_digest,
            candidate.body_digest,
        ):
            raise DefinitionRefused(
                "DEFINITION_VERSION_CONFLICT", {"version": candidate.version}
            )
        return receipt(None)
    if new < old:
        raise DefinitionRefused(
            "DEFINITION_VERSION_CONFLICT",
            {"prior": prior.version, "candidate": candidate.version},
        )
    if decision is None:
        raise DefinitionRefused("PRODUCER_UNAVAILABLE")
    if new[0] == old[0]:
        if decision.verdict != "compatible":
            raise DefinitionRefused(
                "SCHEMA_INCOMPATIBLE", {"decision": decision.decision_id}
            )
    elif not (decision.migration_id and decision.readmitted):
        raise DefinitionRefused(
            "SCHEMA_INCOMPATIBLE",
            {
                "decision": decision.decision_id,
                "reason": "major change needs a migration",
            },
        )
    return receipt(decision.decision_id)


@dataclass(frozen=True)
class DefinitionGeneration:
    """One immutable catalog generation; publishing returns the next one whole."""

    number: int = 0
    definitions: Mapping[str, VersionedDefinition] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "definitions", dict(self.definitions or {}))

    def publish(
        self,
        candidates: Mapping[str, VersionedDefinition],
        decisions: Mapping[str, CompatibilityDecision],
        *,
        tenant: str = "",
    ) -> tuple[DefinitionGeneration, tuple[DefinitionAdmissionReceipt, ...]]:
        """Admit every candidate or none: a refusal raises and this generation stays."""
        assert self.definitions is not None
        merged = dict(self.definitions)
        receipts = []
        for key in sorted(candidates):
            receipts.append(
                admit_definition(
                    self.definitions.get(key),
                    candidates[key],
                    decisions.get(key),
                    tenant=tenant,
                    generation=self.number + 1,
                )
            )
            merged[key] = candidates[key]
        return DefinitionGeneration(self.number + 1, merged), tuple(receipts)


__all__ += ["canonical_digest"]
