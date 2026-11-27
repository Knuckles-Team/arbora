"""Admission records for skills and workflows (ARBORA-FLEET-R034.1, R034.7).

Closed, frozen models. A missing required field refuses with
``ADMISSION_FIELD_MISSING`` and a missing certification binding with
``CERTIFICATION_REQUIRED``; neither refusal runs an effect.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

__all__ = [
    "AdmissionRefused",
    "AuditFinding",
    "CertificationBinding",
    "Digest",
    "SkillAuditReport",
    "WorkflowAdmissionRecord",
    "WorkflowBinding",
    "canonical_digest",
    "parse_certification_binding",
    "parse_workflow_binding",
    "parse_workflow_record",
]

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Severity = Literal["low", "medium", "high"]


class AdmissionRefused(Exception):
    """A refusal with a stable code and the field or limit that caused it."""

    def __init__(self, code: str, detail: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.detail = dict(detail or {})
        super().__init__(f"{code}: {self.detail}")


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def canonical_digest(value: Any) -> str:
    """SHA-256 hex over canonical sorted JSON."""
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode()).hexdigest()


class AuditFinding(_Closed):
    rule: str = Field(min_length=1)
    severity: Severity
    location: str = Field(min_length=1)


class SkillAuditReport(_Closed):
    skill_id: str = Field(min_length=1)
    policy: str = Field(min_length=1)
    limit: int = Field(ge=0, le=50)
    findings: tuple[AuditFinding, ...] = ()

    @property
    def high(self) -> int:
        return sum(1 for item in self.findings if item.severity == "high")

    @property
    def passed(self) -> bool:
        return self.high <= self.limit

    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class DataFlow(_Closed):
    source: str = Field(min_length=1)
    sink: str = Field(min_length=1)


class WorkflowAdmissionRecord(_Closed):
    owner: str = Field(min_length=1)
    inputs_schema: dict[str, Any]
    data_flows: tuple[DataFlow, ...] = Field(min_length=1)
    tool_inventory: tuple[str, ...] = Field(min_length=1)


class CertificationBinding(_Closed):
    skill_id: str = Field(min_length=1)
    skill_version: str = Field(min_length=1)
    dependency_digests: tuple[Digest, ...] = ()
    model_endpoint_revision: str = Field(min_length=1)
    system_prompt_digest: Digest
    attestation_digest: Digest


class WorkflowBinding(_Closed):
    tenant: str = Field(min_length=1)
    caller: str = Field(min_length=1)
    workflow_digest: Digest
    tool_schema_digests: dict[str, Digest]
    grant_revision: str = Field(min_length=1)
    #: skill id -> the skill ids it depends on; atomic skills have none.
    dependencies: dict[str, tuple[str, ...]] = Field(default_factory=dict)


def _missing(error: ValidationError) -> list[str]:
    return sorted({".".join(str(p) for p in e["loc"]) for e in error.errors()})


def _parse(model: type[BaseModel], data: Mapping[str, Any], code: str) -> Any:
    try:
        return model.model_validate(dict(data))
    except ValidationError as error:
        raise AdmissionRefused(code, {"fields": _missing(error)}) from None


def parse_workflow_record(data: Mapping[str, Any]) -> WorkflowAdmissionRecord:
    """Parse a declared workflow; a missing field refuses ``ADMISSION_FIELD_MISSING``."""
    return _parse(WorkflowAdmissionRecord, data, "ADMISSION_FIELD_MISSING")


def parse_certification_binding(data: Mapping[str, Any] | None) -> CertificationBinding:
    if not data:
        raise AdmissionRefused("CERTIFICATION_REQUIRED", {"fields": ["certification"]})
    return _parse(CertificationBinding, data, "CERTIFICATION_REQUIRED")


def parse_workflow_binding(data: Mapping[str, Any] | None) -> WorkflowBinding:
    if not data:
        raise AdmissionRefused("CERTIFICATION_REQUIRED", {"fields": ["binding"]})
    return _parse(WorkflowBinding, data, "CERTIFICATION_REQUIRED")
