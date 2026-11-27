#!/usr/bin/python
from __future__ import annotations

"""Durable, idempotent ScaleIntent reconciliation.

CONCEPT:MERISTEM-OS.scaling.reactive-replica-autoscaling

This module owns the Meristem-side transaction seam between a durable scale intent
and a typed actuator.  It deliberately does not know Kubernetes, Docker,
Swarm, or a graph client's wire shape.  A deployment supplies a durable
``ScaleIntentLedger`` and a typed, idempotent ``ScaleActuator``.

The ordering is intentional and safety-critical:

1. persist the complete intent identity before an actuator is called;
2. acquire the controller lease and persist ``started`` under its fence;
3. call the actuator with the same execution key on every retry;
4. persist the typed result, then persist exactly one observation;
5. return ``verified`` only after the observation is durable.

If a process crashes between steps 3 and 4, the next delivery sees the
``started`` execution and retries the same execution key.  The actuator
contract must make that key idempotent.  If a process crashes after step 4,
the next delivery records the missing observation without calling the
actuator again.  A dry-run uses the explicit ``simulated`` state and can
never claim that replicas changed.
"""

import asyncio
import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from pydantic import (
    StrictBool,
)

from arbora.api.domain.fleet.scale_records import (
    MAX_DETAIL_LENGTH,
    MAX_LEASE_TTL_S,
    BreakGlassAudit,
    BreakGlassAuthorization,
    ControllerLease,
    ScaleActuationRequest,
    ScaleActuationResult,
    ScaleExecutionRecord,
    ScaleIntentConflict,
    ScaleIntentRecord,
    ScaleIntentState,
    ScaleLeaseLost,
    ScaleLeaseUnavailable,
    ScaleModel,
    ScaleObservation,
    ScaleObservationConflict,
    ScaleObservationStatus,
    ScalePolicyDecision,
    ScaleReconcileResult,
    aware_utc,
    checked_identifier,
    derived_id,
    result_digest,
)


@runtime_checkable
class ScaleActuator(Protocol):
    """Typed actuator; implementations must deduplicate ``execution_key``."""

    name: str

    def apply(self, request: ScaleActuationRequest) -> ScaleActuationResult:
        """Apply or idempotently replay one typed request; never return a dict."""
        ...  # ABSTRACT-OK


@runtime_checkable
class ScalePolicy(Protocol):
    async def decide(self, intent: ScaleIntentRecord) -> ScalePolicyDecision:
        """Return a durable-policy verdict before actuation."""
        ...  # ABSTRACT-OK


class AllowScalePolicy:
    """Explicit allow fixture for tests; the host composes the engine verdict instead."""

    async def decide(self, intent: ScaleIntentRecord) -> ScalePolicyDecision:
        return ScalePolicyDecision(
            decision_id=derived_id("decision", intent.execution_key),
            allowed=True,
            reason="allowed by injected test policy",
        )


class DenyScalePolicy:
    """Fail-closed policy used when a deployment has not injected governance."""

    async def decide(self, intent: ScaleIntentRecord) -> ScalePolicyDecision:
        return ScalePolicyDecision(
            decision_id=derived_id("decision", intent.execution_key),
            allowed=False,
            reason="no scale policy was injected",
        )


@runtime_checkable
class ScaleIntentLedger(Protocol):
    """Durable store required by the reconcile ordering contract."""

    async def persist_intent(self, intent: ScaleIntentRecord) -> PersistedIntent:
        """Atomically insert/deduplicate the intent before any actuator call."""
        ...  # ABSTRACT-OK

    async def acquire_controller_lease(
        self,
        intent: ScaleIntentRecord,
        controller_id: str,
        *,
        now: datetime,
    ) -> ControllerLease | None:
        """Return a fenced lease or ``None`` when another controller owns it."""
        ...  # ABSTRACT-OK

    async def mark_started(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        lease: ControllerLease,
        *,
        now: datetime,
    ) -> ScaleExecutionRecord:
        """Persist started state under the live lease before applying a side effect."""
        ...  # ABSTRACT-OK

    async def record_result(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        lease: ControllerLease,
        result: ScaleActuationResult,
        *,
        now: datetime,
    ) -> ScaleExecutionRecord:
        """Persist one typed result idempotently under the execution key."""
        ...  # ABSTRACT-OK

    async def record_observation(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        observation: ScaleObservation,
        *,
        now: datetime,
        lease: ControllerLease | None = None,
    ) -> ScaleObservation:
        """Persist exactly one convergence/failure observation.

        With the live ``lease`` the write is fenced by it and the item is then
        committed to its terminal state while the claim is held.
        """
        ...  # ABSTRACT-OK

    async def get_observation(self, execution_key: str) -> ScaleObservation | None:
        """Read the durable terminal observation for crash/replay repair."""
        ...  # ABSTRACT-OK

    async def record_break_glass(
        self,
        intent: ScaleIntentRecord,
        authorization: BreakGlassAuthorization,
        *,
        now: datetime,
    ) -> BreakGlassAudit:
        """Persist the authorization audit before bypassing a policy denial."""
        ...  # ABSTRACT-OK


class PersistedIntent(ScaleModel):
    execution: ScaleExecutionRecord
    replayed: StrictBool = False


def break_glass_audit(
    intent: ScaleIntentRecord,
    authorization: BreakGlassAuthorization,
    *,
    now: datetime,
    persisted: ScaleExecutionRecord | None,
    existing: BreakGlassAudit | None,
) -> BreakGlassAudit:
    """The audit one authorization yields: validated, and replay-checked by digest.

    A ledger persists the audit it returns when ``existing`` was ``None``; a replay of
    the same authorization returns ``existing`` and a changed one refuses.
    """
    if not authorization.authorized or now >= authorization.authorized_until:
        raise ScaleIntentConflict("break-glass authorization is not active")
    if authorization.target != intent.target:
        raise ScaleIntentConflict("break-glass target does not match intent")
    if persisted is None or persisted.identity_digest != intent.identity_digest():
        raise ScaleIntentConflict("break-glass requires the persisted intent identity")
    material = json.dumps(
        {
            "authorization_id": authorization.authorization_id,
            "intent_id": intent.intent_id,
            "actor_id": authorization.actor_id,
            "approval_ref": authorization.approval_ref,
            "reason": authorization.reason,
            "target": authorization.target.model_dump(mode="json"),
            "authorized_until": authorization.authorized_until.isoformat(),
            "authorized": authorization.authorized,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    audit_digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    if existing is not None:
        if existing.audit_digest != audit_digest:
            raise ScaleIntentConflict(
                "break-glass authorization replay changed its identity"
            )
        return existing
    return BreakGlassAudit(
        audit_id=derived_id("audit", authorization.authorization_id),
        authorization_id=authorization.authorization_id,
        intent_id=intent.intent_id,
        actor_id=authorization.actor_id,
        approval_ref=authorization.approval_ref,
        target=authorization.target,
        reason=authorization.reason,
        authorized_until=authorization.authorized_until,
        recorded_at=now,
        audit_digest=audit_digest,
    )


class MemoryScaleIntentLedger:
    """Deterministic fixture ledger implementing the durable protocol.

    This is intentionally an in-memory test seam, not a production durability
    claim.  A graph/database adapter must preserve the same atomic ordering,
    identity-conflict behavior, fence checks, and terminal idempotency.
    """

    def __init__(self) -> None:
        self._intents: dict[tuple[str, int], tuple[str, str]] = {}
        self._executions: dict[str, ScaleExecutionRecord] = {}
        self._leases: dict[str, ControllerLease] = {}
        self._observations: dict[str, ScaleObservation] = {}
        self._break_glass: dict[tuple[str, str], BreakGlassAudit] = {}
        self.events: list[str] = []
        self._lock = threading.RLock()

    async def persist_intent(self, intent: ScaleIntentRecord) -> PersistedIntent:
        with self._lock:
            identity = intent.identity_digest()
            intent_key = (intent.intent_id, intent.intent_revision)
            prior = self._intents.get(intent_key)
            if prior is not None and prior != (intent.execution_key, identity):
                raise ScaleIntentConflict(
                    "intent revision replayed with different identity"
                )
            existing = self._executions.get(intent.execution_key)
            if existing is not None:
                if existing.identity_digest != identity:
                    raise ScaleIntentConflict(
                        "execution key replayed with different identity"
                    )
                self.events.append("persist_intent:replayed")
                return PersistedIntent(execution=existing, replayed=True)
            if prior is not None and prior[0] != intent.execution_key:
                raise ScaleIntentConflict(
                    "intent revision is bound to another execution key"
                )
            now = intent.created_at
            execution = ScaleExecutionRecord(
                execution_id=derived_id("execution", intent.execution_key),
                intent_id=intent.intent_id,
                intent_revision=intent.intent_revision,
                execution_key=intent.execution_key,
                target=intent.target,
                state=ScaleIntentState.PERSISTED,
                attempt=0,
                dry_run=False,
                actuator_called=False,
                scaled=False,
                result_attempt=None,
                identity_digest=identity,
                created_at=now,
                updated_at=now,
            )
            self._intents[intent_key] = (intent.execution_key, identity)
            self._executions[intent.execution_key] = execution
            self.events.append("persist_intent")
            return PersistedIntent(execution=execution, replayed=False)

    def _resolve_existing_lease(
        self,
        current: ControllerLease | None,
        current_execution: ScaleExecutionRecord,
        controller_id: str,
        now: datetime,
    ) -> tuple[bool, ControllerLease | None]:
        """``(handled, result)`` — ``handled`` True means the caller must
        return ``result`` immediately (a replay or a conflict rejection);
        False means proceed to issue a new lease."""
        if current is None or current.expires_at <= now:
            return False, None
        is_retryable_failed = (
            current_execution.state == ScaleIntentState.FAILED.value
            and current_execution.result is not None
            and current_execution.result.retryable
        )
        if current.controller_id == controller_id and not is_retryable_failed:
            self.events.append("lease:replayed")
            return True, current
        if current.controller_id != controller_id:
            return True, None
        return False, None

    async def acquire_controller_lease(
        self,
        intent: ScaleIntentRecord,
        controller_id: str,
        *,
        now: datetime,
    ) -> ControllerLease | None:
        with self._lock:
            now = aware_utc(now, "now")
            if now >= intent.fence.expires_at:
                raise ScaleLeaseUnavailable("scale intent fence is expired")
            current_execution = self._executions.get(intent.execution_key)
            if (
                current_execution is None
                or current_execution.identity_digest != intent.identity_digest()
            ):
                raise ScaleIntentConflict(
                    "controller lease requires the persisted intent identity"
                )
            current = self._leases.get(intent.execution_key)
            handled, result = self._resolve_existing_lease(
                current, current_execution, controller_id, now
            )
            if handled:
                return result
            prior_attempt = self._executions[intent.execution_key].attempt
            lease = ControllerLease(
                lease_instance_id=f"lease:{uuid.uuid4().hex}",
                controller_id=checked_identifier(controller_id, "controller_id"),
                fence=intent.fence,
                attempt=prior_attempt + 1,
                acquired_at=now,
                expires_at=min(
                    intent.fence.expires_at,
                    now + timedelta(seconds=MAX_LEASE_TTL_S),
                ),
            )
            self._leases[intent.execution_key] = lease
            self.events.append("lease:acquired")
            return lease

    async def mark_started(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        lease: ControllerLease,
        *,
        now: datetime,
    ) -> ScaleExecutionRecord:
        with self._lock:
            self._check_lease(intent, execution, lease, now)
            current = self._executions[intent.execution_key]
            if current.state in {
                ScaleIntentState.SIMULATED.value,
                ScaleIntentState.SUCCEEDED.value,
                ScaleIntentState.OBSERVED.value,
                ScaleIntentState.VERIFIED.value,
            }:
                return current
            if current.state == ScaleIntentState.FAILED.value:
                if current.result is None or not current.result.retryable:
                    raise ScaleIntentConflict(
                        "non-retryable execution cannot be started again"
                    )
                if lease.attempt <= current.attempt:
                    raise ScaleLeaseLost("retry must use a newer controller lease")
            updated = current.model_copy(
                update={
                    "state": ScaleIntentState.STARTED.value,
                    "attempt": lease.attempt,
                    "lease": lease,
                    "updated_at": aware_utc(now, "now"),
                }
            )
            self._executions[intent.execution_key] = updated
            self.events.append("execution:started")
            return updated

    @staticmethod
    def _is_result_replay(
        current: ScaleExecutionRecord,
        lease: ControllerLease,
        result: ScaleActuationResult,
    ) -> bool:
        """``True`` when ``current`` is an exact replay of ``result`` and the
        caller may return it unchanged; raises on a genuine mismatch."""
        if current.result is None:
            return False
        same_result = current.result == result
        same_attempt = current.result_attempt == lease.attempt
        retry_attempt = (
            current.result.retryable
            and current.result_attempt is not None
            and lease.attempt > current.result_attempt
            and current.state == ScaleIntentState.STARTED.value
        )
        if not same_result and not retry_attempt:
            raise ScaleIntentConflict("execution key returned a different result")
        return same_result and same_attempt

    async def record_result(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        lease: ControllerLease,
        result: ScaleActuationResult,
        *,
        now: datetime,
    ) -> ScaleExecutionRecord:
        with self._lock:
            self._check_lease(intent, execution, lease, now)
            if (
                result.execution_key != intent.execution_key
                or result.target != intent.target
            ):
                raise ScaleIntentConflict(
                    "actuator result identity does not match intent"
                )
            current = self._executions[intent.execution_key]
            if self._is_result_replay(current, lease, result):
                return current
            state = result.state
            updated = current.model_copy(
                update={
                    "state": state,
                    "dry_run": state == ScaleIntentState.SIMULATED.value,
                    "actuator_called": state != ScaleIntentState.SIMULATED.value,
                    "scaled": result.scaled,
                    "result_attempt": lease.attempt,
                    "lease": lease,
                    "result": result,
                    "updated_at": aware_utc(now, "now"),
                }
            )
            self._executions[intent.execution_key] = updated
            self.events.append(f"execution:result:{state}")
            return updated

    @staticmethod
    def _validate_observation_identity(
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        observation: ScaleObservation,
        current_execution: ScaleExecutionRecord,
    ) -> None:
        if (
            observation.execution_key != intent.execution_key
            or observation.target != intent.target
        ):
            raise ScaleIntentConflict("observation identity does not match intent")
        if (
            observation.intent_id != intent.intent_id
            or observation.intent_revision != intent.intent_revision
        ):
            raise ScaleIntentConflict("observation intent identity does not match")
        if (
            execution.execution_key != current_execution.execution_key
            or execution.identity_digest != current_execution.identity_digest
        ):
            raise ScaleIntentConflict("observation execution identity is stale")

    @staticmethod
    def _observation_matches_result(
        observation: ScaleObservation, result: ScaleActuationResult
    ) -> bool:
        if result.state == ScaleIntentState.SUCCEEDED.value:
            expected_status = ScaleObservationStatus.CONVERGED.value
        elif result.state == ScaleIntentState.SIMULATED.value:
            expected_status = ScaleObservationStatus.SIMULATED.value
        else:
            expected_status = ScaleObservationStatus.FAILED.value
        return (
            observation.status == expected_status
            and observation.scaled == result.scaled
            and observation.observed_replicas == result.observed_replicas
            and observation.outcome_digest == result_digest(result)
        )

    async def record_observation(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        observation: ScaleObservation,
        *,
        now: datetime,
        lease: ControllerLease | None = None,
    ) -> ScaleObservation:
        del lease  # the fixture ledger has no claim to commit under
        with self._lock:
            current_execution = self._executions.get(intent.execution_key)
            if current_execution is None:
                raise ScaleIntentConflict("observation requires a persisted intent")
            self._validate_observation_identity(
                intent, execution, observation, current_execution
            )
            current = self._observations.get(intent.execution_key)
            if current is not None:
                if current.outcome_digest != observation.outcome_digest:
                    raise ScaleObservationConflict("observation replay changed outcome")
                return current
            if current_execution.result is None:
                raise ScaleIntentConflict(
                    "observation requires a durable actuator result"
                )
            result = current_execution.result
            if not self._observation_matches_result(observation, result):
                raise ScaleObservationConflict(
                    "observation does not match the durable actuator result"
                )
            self._observations[intent.execution_key] = observation
            self._executions[intent.execution_key] = current_execution.model_copy(
                update={
                    "state": ScaleIntentState.VERIFIED.value,
                    "observation_id": observation.observation_id,
                    "updated_at": aware_utc(now, "now"),
                }
            )
            self.events.append("observation:verified")
            return observation

    async def record_break_glass(
        self,
        intent: ScaleIntentRecord,
        authorization: BreakGlassAuthorization,
        *,
        now: datetime,
    ) -> BreakGlassAudit:
        with self._lock:
            now = aware_utc(now, "now")
            persisted = self._executions.get(intent.execution_key)
            key = (intent.execution_key, authorization.authorization_id)
            audit = break_glass_audit(
                intent,
                authorization,
                now=now,
                persisted=persisted,
                existing=self._break_glass.get(key),
            )
            if key not in self._break_glass:
                self._break_glass[key] = audit
                self.events.append("break_glass:audit")
            return audit

    async def get_observation(self, execution_key: str) -> ScaleObservation | None:
        with self._lock:
            return self._observations.get(execution_key)

    @staticmethod
    def _execution_identity_mismatch(
        persisted: ScaleExecutionRecord | None,
        execution: ScaleExecutionRecord,
        intent: ScaleIntentRecord,
    ) -> bool:
        return (
            persisted is None
            or persisted.identity_digest != intent.identity_digest()
            or execution.identity_digest != persisted.identity_digest
            or execution.target != intent.target
            or execution.intent_id != intent.intent_id
            or execution.intent_revision != intent.intent_revision
        )

    def _check_lease(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        lease: ControllerLease,
        now: datetime,
    ) -> None:
        now = aware_utc(now, "now")
        current = self._leases.get(intent.execution_key)
        if current is None or current.lease_instance_id != lease.lease_instance_id:
            raise ScaleLeaseLost("controller lease is no longer current")
        if now >= lease.expires_at:
            raise ScaleLeaseLost("controller lease expired")
        if execution.execution_key != intent.execution_key:
            raise ScaleIntentConflict("execution key is not bound to intent")
        persisted = self._executions.get(intent.execution_key)
        if self._execution_identity_mismatch(persisted, execution, intent):
            raise ScaleIntentConflict("execution identity is not bound to intent")
        if lease.fence != intent.fence:
            raise ScaleLeaseLost("controller fence changed")


class ScaleIntentReconciler:
    """One-at-a-time durable intent reconciler with replay-safe retries."""

    def __init__(
        self,
        ledger: ScaleIntentLedger,
        actuator: ScaleActuator,
        *,
        policy: ScalePolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(ledger, ScaleIntentLedger):
            raise TypeError("scale ledger must implement the durable contract")
        if not isinstance(actuator, ScaleActuator):
            raise TypeError("scale actuator must implement the typed contract")
        self.ledger = ledger
        self.actuator = actuator
        self.policy = policy if policy is not None else DenyScalePolicy()
        if not isinstance(self.policy, ScalePolicy):
            raise TypeError("scale policy must implement the policy contract")
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _reconcile_preflight(
        intent: ScaleIntentRecord, controller_id: str, now: datetime
    ) -> None:
        if controller_id != intent.replica_writer_id:
            raise ScaleLeaseUnavailable("controller is not the declared replica writer")
        if now >= intent.fence.expires_at:
            raise ScaleLeaseUnavailable("scale intent fence is expired")

    async def _validate_dry_run_reuse(
        self, intent: ScaleIntentRecord, execution: ScaleExecutionRecord, dry_run: bool
    ) -> None:
        if not dry_run:
            return
        if execution.state == ScaleIntentState.VERIFIED.value:
            existing = await self._existing_observation(intent.execution_key)
            if (
                existing is None
                or existing.status != ScaleObservationStatus.SIMULATED.value
            ):
                raise ScaleIntentConflict(
                    "dry-run cannot reuse an execution that may have scaled"
                )
        elif execution.state not in {
            ScaleIntentState.PERSISTED.value,
            ScaleIntentState.SIMULATED.value,
        }:
            raise ScaleIntentConflict(
                "dry-run cannot reuse a live or retrying execution key"
            )

    async def _replay_verified_or_observed(
        self, intent: ScaleIntentRecord, execution: ScaleExecutionRecord
    ) -> ScaleReconcileResult | None:
        if execution.state not in {
            ScaleIntentState.OBSERVED.value,
            ScaleIntentState.VERIFIED.value,
        }:
            return None
        observation = await self._existing_observation(intent.execution_key)
        if observation is None:
            raise ScaleObservationConflict(
                "verified execution is missing its durable observation"
            )
        return ScaleReconcileResult(
            state=ScaleIntentState.VERIFIED,
            replayed=True,
            actuator_called=False,
            scaled=bool(observation and observation.scaled),
            execution=execution,
            observation=observation,
            detail="terminal execution replayed",
        )

    async def _repair_lease(
        self, intent: ScaleIntentRecord, controller_id: str, now: datetime
    ) -> ControllerLease | None:
        """The claim a repair writes its observation under, when this controller can hold it."""
        try:
            return await self.ledger.acquire_controller_lease(
                intent, controller_id, now=now
            )
        except (ScaleLeaseUnavailable, ScaleLeaseLost):
            return None

    async def _replay_durable_success(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        now: datetime,
        controller_id: str,
    ) -> ScaleReconcileResult | None:
        if (
            execution.state
            not in {
                ScaleIntentState.SIMULATED.value,
                ScaleIntentState.SUCCEEDED.value,
            }
            or execution.result is None
        ):
            return None
        observation = await self._observe_result(
            intent,
            execution,
            execution.result,
            now,
            lease=await self._repair_lease(intent, controller_id, now),
        )
        return ScaleReconcileResult(
            state=ScaleIntentState.VERIFIED,
            replayed=True,
            actuator_called=False,
            scaled=observation.scaled,
            execution=self._verified_execution(execution, observation),
            observation=observation,
            detail="durable result replayed into missing observation",
        )

    async def _replay_terminal_failure(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        now: datetime,
        controller_id: str,
    ) -> ScaleReconcileResult | None:
        """A durable non-retryable result (or an exhausted retry budget) is
        terminal even if the process died before writing its observation."""
        if execution.state != ScaleIntentState.FAILED.value or execution.result is None:
            return None
        if (
            execution.result.retryable
            and execution.attempt < intent.retry_policy.max_attempts
        ):
            return None
        observation = await self._observe_result(
            intent,
            execution,
            execution.result,
            now,
            lease=await self._repair_lease(intent, controller_id, now),
        )
        return ScaleReconcileResult(
            state=ScaleIntentState.VERIFIED,
            replayed=True,
            actuator_called=False,
            scaled=False,
            execution=self._verified_execution(execution, observation),
            observation=observation,
            detail="terminal actuator result replayed into missing observation",
        )

    @staticmethod
    def _reject_exhausted_retry_budget(
        intent: ScaleIntentRecord, execution: ScaleExecutionRecord
    ) -> None:
        if (
            execution.state == ScaleIntentState.STARTED.value
            and execution.result is None
            and execution.attempt >= intent.retry_policy.max_attempts
        ):
            # The side effect may have happened, but its durable result did
            # not.  Do not exceed the declared retry budget or invent a
            # failure observation that could conceal an external change.
            raise ScaleIntentConflict(
                "retry budget exhausted before an actuator result was durable"
            )

    async def _replay_terminal_execution(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        now: datetime,
        controller_id: str,
    ) -> ScaleReconcileResult | None:
        """Repair/replay a terminal or durably-resolved execution without
        consulting policy or invoking an actuator again — the execution key
        is already consumed. Returns ``None`` when reconcile must continue
        past this point (a fresh scale, or an exhausted retry budget raise)."""
        result = await self._replay_verified_or_observed(intent, execution)
        if result is not None:
            return result
        result = await self._replay_durable_success(
            intent, execution, now, controller_id
        )
        if result is not None:
            return result
        result = await self._replay_terminal_failure(
            intent, execution, now, controller_id
        )
        if result is not None:
            return result
        self._reject_exhausted_retry_budget(intent, execution)
        return None

    async def _decide_policy_and_break_glass(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        persisted: PersistedIntent,
        break_glass: BreakGlassAuthorization | None,
        now: datetime,
    ) -> tuple[ScaleReconcileResult | None, BreakGlassAudit | None]:
        """``(early_result, audit)`` — ``early_result`` is non-``None`` only
        for a policy-denied-without-break-glass response the caller must
        return as-is."""
        decision = await self.policy.decide(intent)
        if decision.allowed:
            return None, None
        if break_glass is None:
            return (
                ScaleReconcileResult(
                    state=ScaleIntentState.DENIED,
                    replayed=persisted.replayed,
                    execution=execution,
                    detail=f"policy denied: {decision.reason}",
                ),
                None,
            )
        self._validate_break_glass(intent, break_glass, now)
        audit = await self.ledger.record_break_glass(intent, break_glass, now=now)
        return None, audit

    async def _execute_dry_run(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        lease: ControllerLease,
        persisted: PersistedIntent,
        audit: BreakGlassAudit | None,
        now: datetime,
    ) -> ScaleReconcileResult:
        result = ScaleActuationResult(
            execution_key=intent.execution_key,
            target=intent.target,
            state=ScaleIntentState.SIMULATED,
            scaled=False,
            detail="dry-run: actuator not called",
        )
        execution = await self.ledger.record_result(
            intent, execution, lease, result, now=now
        )
        observation = await self._observe_result(
            intent, execution, result, now, lease=lease
        )
        return ScaleReconcileResult(
            state=ScaleIntentState.VERIFIED,
            replayed=persisted.replayed,
            actuator_called=False,
            scaled=False,
            execution=self._verified_execution(execution, observation),
            observation=observation,
            break_glass_audit=audit,
            detail="dry-run simulated; no replicas changed",
        )

    async def _invoke_actuator(
        self, intent: ScaleIntentRecord, request: ScaleActuationRequest
    ) -> ScaleActuationResult:
        try:
            result = await asyncio.to_thread(self.actuator.apply, request)
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "typed failure is persisted, never raised as success", exc_info=True
            )
            return ScaleActuationResult(
                execution_key=intent.execution_key,
                target=intent.target,
                state=ScaleIntentState.FAILED,
                scaled=False,
                retryable=True,
                error_code="actuator_exception",
                detail=str(exc)[:MAX_DETAIL_LENGTH],
            )
        if not isinstance(result, ScaleActuationResult):
            raise TypeError("scale actuator returned a non-typed result")
        if result.state == ScaleIntentState.SIMULATED.value:
            raise ScaleIntentConflict(
                "a live actuator must not return a simulated result"
            )
        if (
            result.state == ScaleIntentState.SUCCEEDED.value
            and result.observed_replicas != intent.desired_replicas
        ):
            return ScaleActuationResult(
                execution_key=intent.execution_key,
                target=intent.target,
                state=ScaleIntentState.FAILED,
                scaled=False,
                observed_replicas=result.observed_replicas,
                retryable=False,
                error_code="observed_replicas_mismatch",
                detail="actuator did not converge to the requested replica count",
            )
        return result

    async def _execute_live(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        lease: ControllerLease,
        persisted: PersistedIntent,
        audit: BreakGlassAudit | None,
        now: datetime,
    ) -> ScaleReconcileResult:
        execution = await self.ledger.mark_started(intent, execution, lease, now=now)
        request = ScaleActuationRequest(
            intent_id=intent.intent_id,
            intent_revision=intent.intent_revision,
            expected_unit_revision=intent.expected_unit_revision,
            execution_key=intent.execution_key,
            desired_replicas=intent.desired_replicas,
            controller_mode=intent.controller_mode,
            replica_writer_id=intent.replica_writer_id,
            target=intent.target,
            lease=lease,
            dry_run=False,
        )
        result = await self._invoke_actuator(intent, request)
        execution = await self.ledger.record_result(
            intent, execution, lease, result, now=now
        )
        if (
            result.state == ScaleIntentState.FAILED.value
            and result.retryable
            and lease.attempt < intent.retry_policy.max_attempts
        ):
            return ScaleReconcileResult(
                state=ScaleIntentState.FAILED,
                replayed=persisted.replayed,
                actuator_called=True,
                scaled=False,
                execution=execution,
                break_glass_audit=audit,
                retry_after_s=intent.retry_policy.backoff_s,
                detail="retryable actuator failure persisted",
            )
        observation = await self._observe_result(
            intent, execution, result, now, lease=lease
        )
        return ScaleReconcileResult(
            state=ScaleIntentState.VERIFIED,
            replayed=persisted.replayed,
            actuator_called=True,
            scaled=result.scaled,
            execution=self._verified_execution(execution, observation),
            observation=observation,
            break_glass_audit=audit,
            detail=result.detail,
        )

    async def reconcile(
        self,
        intent: ScaleIntentRecord,
        *,
        controller_id: str,
        dry_run: bool = False,
        break_glass: BreakGlassAuthorization | None = None,
    ) -> ScaleReconcileResult:
        now = aware_utc(self.clock(), "clock")
        self._reconcile_preflight(intent, controller_id, now)
        persisted = await self.ledger.persist_intent(intent)
        execution = persisted.execution
        await self._validate_dry_run_reuse(intent, execution, dry_run)

        replay = await self._replay_terminal_execution(
            intent, execution, now, controller_id
        )
        if replay is not None:
            return replay

        early_result, audit = await self._decide_policy_and_break_glass(
            intent, execution, persisted, break_glass, now
        )
        if early_result is not None:
            return early_result

        lease = await self.ledger.acquire_controller_lease(
            intent, controller_id, now=now
        )
        if lease is None:
            raise ScaleLeaseUnavailable("another controller owns the scale lease")

        if dry_run:
            return await self._execute_dry_run(
                intent, execution, lease, persisted, audit, now
            )

        return await self._execute_live(intent, execution, lease, persisted, audit, now)

    @staticmethod
    def _verified_execution(
        execution: ScaleExecutionRecord,
        observation: ScaleObservation,
    ) -> ScaleExecutionRecord:
        """Reflect the durable observation transition in the returned view."""

        return execution.model_copy(
            update={
                "state": ScaleIntentState.VERIFIED.value,
                "observation_id": observation.observation_id,
            }
        )

    async def _observe_result(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        result: ScaleActuationResult,
        now: datetime,
        *,
        lease: ControllerLease | None = None,
    ) -> ScaleObservation:
        status = (
            ScaleObservationStatus.CONVERGED
            if result.state == ScaleIntentState.SUCCEEDED.value
            else ScaleObservationStatus.SIMULATED
            if result.state == ScaleIntentState.SIMULATED.value
            else ScaleObservationStatus.FAILED
        )
        outcome_digest = result_digest(result)
        observation = ScaleObservation(
            observation_id=derived_id("observation", intent.execution_key),
            execution_key=intent.execution_key,
            intent_id=intent.intent_id,
            intent_revision=intent.intent_revision,
            target=intent.target,
            status=status,
            scaled=result.scaled,
            observed_replicas=result.observed_replicas,
            outcome_digest=outcome_digest,
            observed_at=now,
            detail=result.detail,
        )
        return await self.ledger.record_observation(
            intent,
            execution,
            observation,
            now=now,
            lease=lease,
        )

    async def _observe_failure(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        now: datetime,
    ) -> ScaleObservation:
        if execution.result is None:
            raise ScaleIntentConflict("terminal failure lacks durable actuator result")
        return await self._observe_result(intent, execution, execution.result, now)

    async def _existing_observation(
        self, execution_key: str
    ) -> ScaleObservation | None:
        return await self.ledger.get_observation(execution_key)

    @staticmethod
    def _validate_break_glass(
        intent: ScaleIntentRecord,
        authorization: BreakGlassAuthorization,
        now: datetime,
    ) -> None:
        if not authorization.authorized:
            raise ScaleIntentConflict("break-glass authorization is not active")
        if authorization.target != intent.target:
            raise ScaleIntentConflict("break-glass authorization target mismatch")
        if now >= authorization.authorized_until:
            raise ScaleIntentConflict("break-glass authorization is expired")


__all__ = [
    "AllowScalePolicy",
    "DenyScalePolicy",
    "MemoryScaleIntentLedger",
    "PersistedIntent",
    "ScaleActuator",
    "ScaleIntentLedger",
    "ScaleIntentReconciler",
    "ScalePolicy",
    "break_glass_audit",
]
