"""Provisioning at scale through the SDK provisioner (ARBORA-FLEET-R024.5, R024.7, R024.8).

``provision_instances`` validates the platform matrix and the guards, acquires
capacity from the :class:`CapacityLedger` before any allocation, allocates through the
scion-sdk provisioner (``SCION-CONNECTOR-CONTROL-R906``) and records a
``FleetCapacityProof`` into the run journal. Every refusal happens before allocation.
An unknown allocation outcome keeps its capacity and requires reconciliation; it is
never repeated. Arbora keeps no allocation store: replay fencing is the SDK's.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from arbora.api.contracts.provisioning_capacity import (
    AggregationRef,
    FleetCapacityProof,
    ProvisionRequest,
    ProvisionResult,
)
from arbora.control_plane.policy.models import canonical_digest
from arbora.control_plane.runs.audit import AuditChain
from arbora.control_plane.runs.capacity import (
    CapacityLedger,
    CapacityReDecisionExhaustedError,
)
from arbora.core.provision_errors import ProvisionRefused
from arbora.pin_lag.scion_sdk import require_provisioning

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_CONCURRENT_INSTANCES",
    "PROVISION_PROOF_KIND",
    "ProvisionGuards",
    "max_concurrent_instances",
    "provision_instances",
]

PROVISION_PROOF_KIND = "fleet.capacity.proof"
DEFAULT_MAX_CONCURRENT_INSTANCES = 200


@dataclass(frozen=True)
class ProvisionGuards:
    """The checks that run before capacity or allocation."""

    matrix: frozenset[tuple[str, str]]
    grant: Callable[[ProvisionRequest], bool]
    decision_fresh: Callable[[], bool]


def max_concurrent_instances(config: Mapping[str, Any]) -> int:
    """``provisioning.max_concurrent_instances``: instances, from 1 to 2000, default 200."""
    value = (config.get("provisioning") or {}).get(
        "max_concurrent_instances", DEFAULT_MAX_CONCURRENT_INSTANCES
    )
    if type(value) is not int or not 1 <= value <= 2000:
        raise ValueError("provisioning.max_concurrent_instances must be 1 to 2000")
    return value


def _check_guards(request: ProvisionRequest, guards: ProvisionGuards, cap: int) -> None:
    if (request.os, request.runtime) not in guards.matrix:
        raise ProvisionRefused("PLATFORM_UNSUPPORTED")
    if not guards.grant(request):
        raise ProvisionRefused("PROVISION_GRANT_DENIED")
    if not guards.decision_fresh():
        raise ProvisionRefused("PROVISION_DECISION_STALE")
    if request.instance_count > request.budget or request.instance_count > cap:
        raise ProvisionRefused("PROVISION_BUDGET_EXCEEDED")


def _acquire(ledger: CapacityLedger, holder: str, count: int) -> None:
    if ledger.held_by(holder) >= count:
        return  # a replay: the capacity is already held for this key
    try:
        decision = ledger.acquire(holder, count)
    except CapacityReDecisionExhaustedError:
        raise ProvisionRefused("PROVISION_BUDGET_EXCEEDED", exhausted=True) from None
    if not decision.granted:
        raise ProvisionRefused("PROVISION_BUDGET_EXCEEDED", redecision=True)


def _allocate(sdk: Any, provisioner: Any, request: ProvisionRequest) -> Any:
    sdk_request = sdk.AllocationRequest(
        idempotency_key=request.idempotency_key,
        image=request.image_digest,
        platform=request.platform,
        count=request.instance_count,
    )
    try:
        return provisioner.allocate(sdk_request)
    except sdk.ProvisionError as exc:
        if exc.code == "IDEMPOTENCY_CONFLICT":
            raise ProvisionRefused("PROVISION_REPLAY_BODY_CHANGED") from None
        if exc.code == "PLATFORM_UNSUPPORTED":
            raise ProvisionRefused("PLATFORM_UNSUPPORTED") from None
        raise ProvisionRefused(
            "PROVISION_OUTCOME_UNKNOWN", allocation_id=exc.allocation_id
        ) from None
    except Exception:
        logger.warning("_allocate failed", exc_info=True)
        raise ProvisionRefused("PROVISION_OUTCOME_UNKNOWN") from None


def _record_proof(
    journal: AuditChain,
    proof: FleetCapacityProof,
    key: str,
    now: int,
) -> None:
    subject = "provision:" + hashlib.sha256(key.encode()).hexdigest()[:32]
    journal.append(
        kind=PROVISION_PROOF_KIND,
        subject_ref=subject,
        payload_digest=canonical_digest(proof.model_dump(mode="json")),
        observed_at=now,
    )


def provision_instances(
    request: ProvisionRequest,
    *,
    provisioner: Any,
    ledger: CapacityLedger,
    guards: ProvisionGuards,
    journal: AuditChain,
    clock: Callable[[], int],
    observe: Callable[[tuple[str, ...]], Mapping[str, str]] | None = None,
    aggregation: AggregationRef | None = None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_INSTANCES,
) -> ProvisionResult:
    """Allocate ``request`` once; refuse before any allocation when a guard fails."""
    sdk = require_provisioning()
    started_at = clock()
    _check_guards(request, guards, max_concurrent)
    holder = request.idempotency_key
    held_before = ledger.held_by(holder)
    _acquire(ledger, holder, request.instance_count)
    try:
        receipt = _allocate(sdk, provisioner, request)
    except ProvisionRefused as refused:
        if refused.code != "PROVISION_OUTCOME_UNKNOWN" and held_before == 0:
            ledger.release(
                holder
            )  # nothing was allocated; an unknown outcome keeps its hold
        raise
    ids = tuple(receipt.instance_ids)
    observed = dict(observe(ids)) if observe is not None else {}
    outcomes = {i: observed.get(i, "started") for i in ids}
    shortfall = max(request.instance_count - len(ids), 0)
    result = ProvisionResult(
        instance_ids=ids,
        allocation_receipt_digest=receipt.digest,
        outcomes=outcomes,
        shortfall=shortfall,
    )
    failed = tuple(i for i, o in outcomes.items() if o == "failed")
    proof = FleetCapacityProof(
        requested_count=request.instance_count,
        admitted_ids=ids,
        started_ids=tuple(i for i, o in outcomes.items() if o != "failed"),
        completed_ids=tuple(i for i, o in outcomes.items() if o == "completed"),
        failed_ids=failed,
        peak_concurrency=ledger.held_by(holder),
        budget_consumed=len(ids),
        started_at=started_at,
        finished_at=clock(),
        receipt_digests=(receipt.digest,),
        aggregation=aggregation,
    )
    _record_proof(journal, proof, request.idempotency_key, clock())
    return result
