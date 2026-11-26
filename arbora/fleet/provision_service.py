"""The composed provisioning service the A2A authority and router call (ARBORA-FLEET-R024.6, R027.3, R005.3).

It binds the SDK provisioner, the capacity ledger, the guards and the run journal once, so
``WorkItemA2AAuthority`` and ``OrchestratorA2ARouter`` reach ``provision_instances`` and
``provision_for_work_item`` through one object. Arbora holds no allocation store here;
the single-use control lease is consumed through the engine-backed ``LeasePort``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from arbora.a2a.work_items import WorkItemView
from arbora.api.contracts.provisioning_capacity import (
    AggregationRef,
    ProvisionRequest,
    ProvisionResult,
)
from arbora.control_plane.runs.audit import AuditChain
from arbora.control_plane.runs.capacity import CapacityLedger
from arbora.fleet.provision_flow import FlowPorts, provision_for_work_item
from arbora.fleet.provision_receipt import IntegratedProvisionReceipt
from arbora.fleet.provisioning import (
    DEFAULT_MAX_CONCURRENT_INSTANCES,
    ProvisionGuards,
    provision_instances,
)

__all__ = ["LEASE_CONSUMED", "LeaseAlreadyConsumed", "LeasePort", "ProvisioningService"]

LEASE_CONSUMED = "LEASE_ALREADY_CONSUMED"


class LeaseAlreadyConsumed(RuntimeError):
    code = LEASE_CONSUMED

    def __init__(self) -> None:
        super().__init__(LEASE_CONSUMED)


class LeasePort(Protocol):
    def consume(self, lease_id: str, work_item_id: str) -> bool:
        """True once per lease; the engine holds the pending-call record."""
        ...  # ABSTRACT-OK


@dataclass
class ProvisioningService:
    provisioner: Any
    ledger: CapacityLedger
    guards: ProvisionGuards
    journal: AuditChain
    clock: Callable[[], int]
    ports: FlowPorts | None = None
    leases: LeasePort | None = None
    observe: Callable[[tuple[str, ...]], Mapping[str, str]] | None = None
    aggregation: AggregationRef | None = None
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_INSTANCES

    def provision(self, request: ProvisionRequest) -> ProvisionResult:
        return provision_instances(
            request,
            provisioner=self.provisioner,
            ledger=self.ledger,
            guards=self.guards,
            journal=self.journal,
            clock=self.clock,
            observe=self.observe,
            aggregation=self.aggregation,
            max_concurrent=self.max_concurrent,
        )

    def for_work_item(
        self,
        view: WorkItemView,
        request: ProvisionRequest,
        *,
        lease_id: str | None = None,
    ) -> IntegratedProvisionReceipt:
        """One work item through discovery, distribution and provisioning."""
        if self.ports is None:
            raise RuntimeError("the provisioning flow ports are not composed")
        if lease_id is not None:
            if self.leases is None or not self.leases.consume(
                lease_id, view.work_item_id
            ):
                raise LeaseAlreadyConsumed
        return provision_for_work_item(
            view,
            ports=self.ports,
            provision=self.provision,
            request=request,
            journal=self.journal,
            clock=self.clock,
        )
