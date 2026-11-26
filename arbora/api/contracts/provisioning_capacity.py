"""Typed provisioning-at-scale contracts (ARBORA-FLEET-R024.4, R024.8, R024.11).

The request, the result and the capacity proof of ``arbora/fleet/provisioning.py``.
The producer is the scion-sdk provisioner (``SCION-CONNECTOR-CONTROL-R906``); these
models only carry what Arbora binds around it.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AggregationRef",
    "FleetCapacityProof",
    "ProvisionRequest",
    "ProvisionResult",
]

type Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProvisionRequest(_Model):
    image_digest: Sha256
    os: str = Field(min_length=1, max_length=64)
    runtime: str = Field(min_length=1, max_length=64)
    tenant: str = Field(min_length=1, max_length=256)
    instance_count: int = Field(ge=1, le=2000)
    budget: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)

    @property
    def platform(self) -> str:
        return f"{self.os}/{self.runtime}"


class ProvisionResult(_Model):
    """Real instance ids from the allocation receipt; a shortfall keeps the ids for cleanup."""

    instance_ids: tuple[str, ...]
    allocation_receipt_digest: Sha256
    outcomes: dict[str, str]
    shortfall: int = Field(default=0, ge=0)


class AggregationRef(_Model):
    """The engine aggregation applied to the instance receipts (ARBORA-FLEET-R024.11)."""

    operation: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    input_receipt_ids: tuple[str, ...] = Field(min_length=1)


class FleetCapacityProof(_Model):
    requested_count: int = Field(ge=1)
    admitted_ids: tuple[str, ...]
    started_ids: tuple[str, ...]
    completed_ids: tuple[str, ...]
    failed_ids: tuple[str, ...]
    peak_concurrency: int = Field(ge=0)
    budget_consumed: int = Field(ge=0)
    started_at: int = Field(ge=0)
    finished_at: int = Field(ge=0)
    receipt_digests: tuple[Sha256, ...] = Field(min_length=1)
    aggregation: AggregationRef | None = None
