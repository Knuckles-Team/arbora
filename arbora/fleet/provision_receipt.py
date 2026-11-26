"""The integrated provisioning receipt (ARBORA-FLEET-R027.1).

One receipt binds the discovery generation, the engine skill identity and version,
the distribution decision, the admission receipt, the instance and the durable
result digest. Every phase carries one tenant, one run id and the exact skill digest;
any mismatch refuses ``RECEIPT_BINDING_MISMATCH``.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["BINDING_MISMATCH", "IntegratedProvisionReceipt", "PhaseBinding"]

BINDING_MISMATCH = "RECEIPT_BINDING_MISMATCH"

type Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PhaseBinding(_Model):
    phase: str = Field(min_length=1, max_length=64)
    tenant: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=512)
    skill_digest: Sha256


class IntegratedProvisionReceipt(_Model):
    tenant: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=512)
    skill_digest: Sha256
    skill_id: str = Field(min_length=1, max_length=256)
    skill_version: str = Field(min_length=1, max_length=64)
    discovery_generation: str = Field(min_length=1, max_length=128)
    distribution_decision_id: str = Field(min_length=1, max_length=256)
    admission_receipt_id: str = Field(min_length=1, max_length=256)
    instance_id: str = Field(min_length=1, max_length=256)
    result_digest: Sha256
    phases: tuple[PhaseBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _phases_agree(self) -> IntegratedProvisionReceipt:
        for phase in self.phases:
            if (phase.tenant, phase.run_id, phase.skill_digest) != (
                self.tenant,
                self.run_id,
                self.skill_digest,
            ):
                raise ValueError(BINDING_MISMATCH)
        return self
