"""The scale intent ledger over the engine's durable WorkItems (ARBORA-FLEET-R024.14).

One WorkItem holds one execution key. The engine owns what makes the ledger durable:
the idempotency key refuses a second identity for one key, claim, lease and expiry
fence the controller, and the item's metadata holds the execution, its lease, the
observation and the break-glass audits. A write under a lease goes through a fenced
compare-and-set, so a controller whose lease was lost or reclaimed loses. Arbora
keeps no state of its own: every method reads the item and writes it back.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from arbora import work_items
from arbora.api.invoke.pipeline import OperationRefused
from arbora.fleet.scaling.scaling_intent_reconciler import (
    MAX_LEASE_TTL_S,
    BreakGlassAudit,
    BreakGlassAuthorization,
    ControllerLease,
    MemoryScaleIntentLedger,
    PersistedIntent,
    ScaleActuationResult,
    ScaleExecutionRecord,
    ScaleIntentConflict,
    ScaleIntentRecord,
    ScaleIntentState,
    ScaleLeaseLost,
    ScaleLeaseUnavailable,
    ScaleObservation,
    ScaleObservationConflict,
    _aware,
    _derived_id,
    break_glass_audit,
)

KIND = "scale_intent"
REVISION_KIND = "scale_intent_revision"
STATE_KEY = "scale"
_CAS_ATTEMPTS = 3

#: States from which a started execution is not started again.
_SETTLED = frozenset(
    {
        ScaleIntentState.SIMULATED.value,
        ScaleIntentState.SUCCEEDED.value,
        ScaleIntentState.OBSERVED.value,
        ScaleIntentState.VERIFIED.value,
    }
)


def _item_id(kind: str, key: str) -> str:
    return f"workitem:{kind}:{hashlib.sha256(key.encode()).hexdigest()[:40]}"


def _state(item: Mapping[str, Any]) -> dict[str, Any]:
    return dict((item.get("metadata") or {}).get(STATE_KEY) or {})


def _execution(state: Mapping[str, Any]) -> ScaleExecutionRecord:
    return ScaleExecutionRecord.model_validate(state["execution"])


def _claim(lease: ControllerLease) -> dict[str, Any]:
    """The engine fence a lease names: ``lease:<epoch>:<fencing token>``."""
    _, epoch, token = lease.lease_instance_id.split(":")
    return {
        "lease_holder_ref": lease.controller_id,
        "lease_epoch": int(epoch),
        "fencing_token": int(token),
    }


class WorkItemScaleIntentLedger:
    """``ScaleIntentLedger`` over the served engine's WorkItem methods.

    ``engine`` is the served engine handle the WorkItem verbs run through; the verified
    session of the calling request supplies the tenant and context.
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def _run(self, verb: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(verb, self._engine, *args, **kwargs)

    async def _read(self, kind: str, key: str) -> dict[str, Any] | None:
        return await self._run(work_items.get_work_item, _item_id(kind, key))

    async def _submit(
        self, kind: str, key: str, state: dict[str, Any], *, max_attempts: int
    ) -> tuple[str, bool]:
        try:
            return await self._run(
                work_items.submit_work_item_atomic,
                kind=kind,
                idempotency_key=key,
                work_item_id=_item_id(kind, key),
                max_attempts=max_attempts,
                metadata={STATE_KEY: state},
            )
        except OperationRefused as refused:
            if refused.code == "IDEMPOTENCY_CONFLICT":
                raise ScaleIntentConflict(
                    "execution key replayed with different identity"
                ) from refused
            raise

    async def _store(
        self,
        item: Mapping[str, Any],
        state: dict[str, Any],
        claim: Mapping[str, Any] | None,
    ) -> bool:
        """Replace the item's state if the item is unchanged and the fence holds."""
        metadata = {**(item.get("metadata") or {}), STATE_KEY: state}
        return bool(
            await self._run(
                work_items.cas_work_item_metadata,
                item,
                metadata=metadata,
                claim=claim,
            )
        )

    async def _item(self, intent: ScaleIntentRecord) -> dict[str, Any]:
        item = await self._read(KIND, intent.execution_key)
        if item is None or _state(item).get("identity") != intent.identity_digest():
            raise ScaleIntentConflict(
                "controller lease requires the persisted intent identity"
            )
        return item

    async def persist_intent(self, intent: ScaleIntentRecord) -> PersistedIntent:
        identity = intent.identity_digest()
        attempts = intent.retry_policy.max_attempts
        binding = {"execution_key": intent.execution_key, "identity": identity}
        revision_key = f"{intent.intent_id}:{intent.intent_revision}"
        await self._submit(REVISION_KIND, revision_key, binding, max_attempts=1)
        now = intent.created_at
        execution = ScaleExecutionRecord(
            execution_id=_derived_id("execution", intent.execution_key),
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
        state = {"identity": identity, "execution": execution.model_dump(mode="json")}
        _, created = await self._submit(
            KIND, intent.execution_key, state, max_attempts=attempts
        )
        if created:
            return PersistedIntent(execution=execution, replayed=False)
        item = await self._item(intent)
        return PersistedIntent(execution=_execution(_state(item)), replayed=True)

    async def acquire_controller_lease(
        self, intent: ScaleIntentRecord, controller_id: str, *, now: datetime
    ) -> ControllerLease | None:
        now = _aware(now, "now")
        if now >= intent.fence.expires_at:
            raise ScaleLeaseUnavailable("scale intent fence is expired")
        item = await self._item(intent)
        held = self._own_live_lease(_state(item), controller_id, now)
        if held is not None:
            return held
        ttl = min(MAX_LEASE_TTL_S, (intent.fence.expires_at - now).total_seconds())
        granted = await self._run(
            work_items.claim_specific,
            item["work_item_id"],
            token=controller_id,
            lease_ttl_s=ttl,
        )
        if granted is None:
            return None
        state = _state(item)
        lease = ControllerLease(
            lease_instance_id=(
                f"lease:{int(granted['lease_epoch'])}:{int(granted['fencing_token'])}"
            ),
            controller_id=controller_id,
            fence=intent.fence,
            attempt=_execution(state).attempt + 1,
            acquired_at=now,
            expires_at=min(intent.fence.expires_at, now + timedelta(seconds=ttl)),
        )
        fresh = await self._read(KIND, intent.execution_key) or item
        if not await self._store(
            fresh, {**state, "lease": lease.model_dump(mode="json")}, _claim(lease)
        ):
            raise ScaleLeaseLost("controller lease is no longer current")
        return lease

    @staticmethod
    def _own_live_lease(
        state: Mapping[str, Any], controller_id: str, now: datetime
    ) -> ControllerLease | None:
        """The controller's own unexpired lease to hand back on a replay.

        Another controller's lease is not read here: the engine's claim is the arbiter,
        refusing while the holder's lease lives and granting once it lapses. A lease
        whose execution failed retryably was released, so it is not replayed.
        """
        stored = state.get("lease")
        if stored is None:
            return None
        current = ControllerLease.model_validate(stored)
        if current.expires_at <= now or current.controller_id != controller_id:
            return None
        result = _execution(state).result
        retry = (
            result is not None
            and result.retryable
            and result.state == (ScaleIntentState.FAILED.value)
        )
        return None if retry else current

    def _check_lease(
        self,
        item: Mapping[str, Any],
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        lease: ControllerLease,
        now: datetime,
    ) -> dict[str, Any]:
        now = _aware(now, "now")
        state = _state(item)
        stored = state.get("lease")
        if stored is None or stored["lease_instance_id"] != lease.lease_instance_id:
            raise ScaleLeaseLost("controller lease is no longer current")
        if now >= lease.expires_at:
            raise ScaleLeaseLost("controller lease expired")
        if execution.execution_key != intent.execution_key:
            raise ScaleIntentConflict("execution key is not bound to intent")
        if MemoryScaleIntentLedger._execution_identity_mismatch(
            _execution(state), execution, intent
        ):
            raise ScaleIntentConflict("execution identity is not bound to intent")
        if lease.fence != intent.fence:
            raise ScaleLeaseLost("controller fence changed")
        return state

    async def _write_under_lease(
        self, item: Mapping[str, Any], state: dict[str, Any], lease: ControllerLease
    ) -> None:
        if not await self._store(item, state, _claim(lease)):
            raise ScaleLeaseLost("controller lease is no longer current")

    async def mark_started(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        lease: ControllerLease,
        *,
        now: datetime,
    ) -> ScaleExecutionRecord:
        item = await self._item(intent)
        state = self._check_lease(item, intent, execution, lease, now)
        current = _execution(state)
        if current.state in _SETTLED:
            return current
        if current.state == ScaleIntentState.FAILED.value:
            if current.result is None or not current.result.retryable:
                raise ScaleIntentConflict(
                    "non-retryable execution cannot be started again"
                )
            if lease.attempt <= current.attempt:
                raise ScaleLeaseLost("retry must use a newer controller lease")
        try:
            await self._run(
                work_items.mark_effect_started,
                item["work_item_id"],
                _claim(lease),
                expected_revision=int(item["version"]),
            )
        except Exception as refused:
            raise ScaleLeaseLost("the engine refused the effect marker") from refused
        updated = current.model_copy(
            update={
                "state": ScaleIntentState.STARTED.value,
                "attempt": lease.attempt,
                "lease": lease,
                "updated_at": _aware(now, "now"),
            }
        )
        fresh = await self._read(KIND, intent.execution_key) or item
        await self._write_under_lease(
            fresh, {**state, "execution": updated.model_dump(mode="json")}, lease
        )
        return updated

    async def record_result(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        lease: ControllerLease,
        result: ScaleActuationResult,
        *,
        now: datetime,
    ) -> ScaleExecutionRecord:
        item = await self._item(intent)
        state = self._check_lease(item, intent, execution, lease, now)
        if (
            result.execution_key != intent.execution_key
            or result.target != intent.target
        ):
            raise ScaleIntentConflict("actuator result identity does not match intent")
        current = _execution(state)
        if MemoryScaleIntentLedger._is_result_replay(current, lease, result):
            return current
        simulated = result.state == ScaleIntentState.SIMULATED.value
        updated = current.model_copy(
            update={
                "state": result.state,
                "dry_run": simulated,
                "actuator_called": not simulated,
                "scaled": result.scaled,
                "result_attempt": lease.attempt,
                "lease": lease,
                "result": result,
                "updated_at": _aware(now, "now"),
            }
        )
        await self._write_under_lease(
            item, {**state, "execution": updated.model_dump(mode="json")}, lease
        )
        if result.state == ScaleIntentState.FAILED.value and result.retryable:
            await self._run(
                work_items.defer_work_item,
                item["work_item_id"],
                _claim(lease),
                next_retry_at=_aware(now, "now").timestamp(),
                reason_ref=result.error_code,
            )
        return updated

    async def record_observation(
        self,
        intent: ScaleIntentRecord,
        execution: ScaleExecutionRecord,
        observation: ScaleObservation,
        *,
        now: datetime,
    ) -> ScaleObservation:
        for _ in range(_CAS_ATTEMPTS):
            item = await self._read(KIND, intent.execution_key)
            if item is None:
                raise ScaleIntentConflict("observation requires a persisted intent")
            state = _state(item)
            current = _execution(state)
            MemoryScaleIntentLedger._validate_observation_identity(
                intent, execution, observation, current
            )
            stored = state.get("observation")
            if stored is not None:
                existing = ScaleObservation.model_validate(stored)
                if existing.outcome_digest != observation.outcome_digest:
                    raise ScaleObservationConflict("observation replay changed outcome")
                return existing
            if current.result is None:
                raise ScaleIntentConflict(
                    "observation requires a durable actuator result"
                )
            if not MemoryScaleIntentLedger._observation_matches_result(
                observation, current.result
            ):
                raise ScaleObservationConflict(
                    "observation does not match the durable actuator result"
                )
            verified = current.model_copy(
                update={
                    "state": ScaleIntentState.VERIFIED.value,
                    "observation_id": observation.observation_id,
                    "updated_at": _aware(now, "now"),
                }
            )
            updated = {
                **state,
                "execution": verified.model_dump(mode="json"),
                "observation": observation.model_dump(mode="json"),
            }
            if await self._store(item, updated, None):
                return observation
        raise ScaleObservationConflict("observation lost a concurrent write")

    async def get_observation(self, execution_key: str) -> ScaleObservation | None:
        item = await self._read(KIND, execution_key)
        stored = None if item is None else _state(item).get("observation")
        return None if stored is None else ScaleObservation.model_validate(stored)

    async def record_break_glass(
        self,
        intent: ScaleIntentRecord,
        authorization: BreakGlassAuthorization,
        *,
        now: datetime,
    ) -> BreakGlassAudit:
        for _ in range(_CAS_ATTEMPTS):
            item = await self._read(KIND, intent.execution_key)
            state = {} if item is None else _state(item)
            audits = dict(state.get("break_glass") or {})
            stored = audits.get(authorization.authorization_id)
            audit = break_glass_audit(
                intent,
                authorization,
                now=_aware(now, "now"),
                persisted=_execution(state) if state else None,
                existing=None
                if stored is None
                else BreakGlassAudit.model_validate(stored),
            )
            if stored is not None or item is None:
                return audit
            audits[authorization.authorization_id] = audit.model_dump(mode="json")
            if await self._store(item, {**state, "break_glass": audits}, None):
                return audit
        raise ScaleIntentConflict("break-glass audit lost a concurrent write")


__all__ = ["KIND", "REVISION_KIND", "WorkItemScaleIntentLedger"]
