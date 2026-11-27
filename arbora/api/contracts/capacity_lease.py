"""Params and results of the build-slot lease operations (ARBORA-CAPACITY-R754.1).

The engine owns the lease, its seven-state machine and every transition. These models
carry only what a caller may choose. No model has an owner, tenant or digest field: the
holder is the verified caller (ARBORA-CAPACITY-R754.2), and ``extra="forbid"`` refuses a
request that names one.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LeaseRequestParams(_Model):
    """Ask for one slot of a cell; the engine validates the priority text."""

    cell_id: str = Field(min_length=1, max_length=256)
    priority: str = Field(min_length=1, max_length=64)
    work_item_id: str = Field(min_length=1, max_length=256)
    ttl_ms: int | None = Field(default=None, ge=1, le=86_400_000)


class LeaseFenceParams(_Model):
    """The fence of a held lease: start, renew and release name it."""

    lease_id: str = Field(min_length=1, max_length=256)
    lease_epoch: int = Field(ge=0)
    fence_token: int = Field(ge=0)
    ttl_ms: int | None = Field(default=None, ge=1, le=86_400_000)


class LeaseRevokeParams(_Model):
    """An administrator takes one lease back; the engine bounds the reason."""

    lease_id: str = Field(min_length=1, max_length=256)
    reason: str = Field(max_length=200)


class LeaseGetParams(_Model):
    lease_id: str = Field(min_length=1, max_length=256)


class LeaseResult(_Model):
    """The lease as the engine records it, with its queue place while it waits."""

    lease: dict[str, Any]
    queue_position: int | None = None


class LeaseStatusResult(_Model):
    """The lease with its newest transitions, oldest first."""

    lease: dict[str, Any]
    transitions: list[dict[str, Any]]


__all__ = [
    "LeaseFenceParams",
    "LeaseGetParams",
    "LeaseRequestParams",
    "LeaseResult",
    "LeaseRevokeParams",
    "LeaseStatusResult",
]
