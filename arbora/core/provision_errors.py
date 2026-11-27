"""The closed refusal vocabulary of capacity provisioning, shared by the host and the fleet."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

__all__ = ["REFUSALS", "ProvisionRefused"]


REFUSALS: Mapping[str, str] = MappingProxyType(
    {
        "PLATFORM_UNSUPPORTED": "the os and runtime are not a declared matrix row",
        "PROVISION_GRANT_DENIED": "the caller holds no grant to provision",
        "PROVISION_DECISION_STALE": "the engine decision behind the request is stale",
        "PROVISION_BUDGET_EXCEEDED": "the request exceeds its budget or the capacity pool",
        "PROVISION_REPLAY_BODY_CHANGED": "the idempotency key was used with another body",
        "PROVISION_OUTCOME_UNKNOWN": "the allocation outcome is unknown; reconcile, never repeat",
    }
)


class ProvisionRefused(RuntimeError):
    """A closed refusal; ``details`` keeps real resource ids needed for cleanup."""

    def __init__(self, code: str, **details: Any) -> None:
        if code not in REFUSALS:
            raise ValueError(f"unknown provisioning refusal {code!r}")
        self.code = code
        self.details = details
        super().__init__(code)
