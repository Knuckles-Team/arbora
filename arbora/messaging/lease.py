"""Durable single-owner leases for messaging inbound intake.

Credentials make a backend usable for governed outbound sends; they do not, by
themselves, grant permission to open an inbound poller.  An embedded intake
owner must acquire the deterministic WorkItem for each ``(platform, bot)``
identity.  The engine-native claim/renew/reclaim verbs remain the only lease
authority, so another process can take over after an expired owner lease.
"""

from __future__ import annotations

import contextvars
import dataclasses
import enum
import hashlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType
from typing import Any

from arbora.core.work_item_errors import (
    NativeWorkItemRequired,
    WorkItemBackendUnavailable,
)

logger = logging.getLogger(__name__)


#: The wall clock in seconds; tests replace it.
_clock: Callable[[], float] = time.time


def _now_ms() -> int:
    return int(_clock() * 1000)


def _work_items(engine: Any) -> Any:
    """The engine's served WorkItem namespace (submit, get, claim, renew, defer)."""
    for holder in (engine, getattr(engine, "_client", None)):
        namespace = getattr(holder, "work_items", None) if holder is not None else None
        if namespace is not None:
            return namespace
    raise NativeWorkItemRequired("the knowledge engine serves no WorkItem methods")


def _request_context(engine: Any) -> dict[str, Any]:
    """The verified request context the engine bound to this client."""
    for holder in (engine, getattr(engine, "_client", None)):
        read = getattr(holder, "_effective_verified_context", None)
        if callable(read):
            return dict(read())
    raise NativeWorkItemRequired("WorkItem submission needs a verified request context")


def _submit_lease_row(
    engine: Any, *, item_id: str, tenant: str, metadata: dict[str, Any]
) -> None:
    """Admit the lease row once; a replay of the same row is accepted."""
    body = {"kind": _LEASE_KIND, "tenant": tenant, "metadata": metadata}
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _work_items(engine).submit(
        {
            "schema_version": "1",
            "context": _request_context(engine),
            "work_item_id": item_id,
            "idempotency_key": item_id,
            "command_digest": digest,
            "kind": _LEASE_KIND,
            "priority": 0,
            "depends_on": [],
            "input_ref": item_id,
            "policy_digest": _LEASE_QUEUE,
            "catalog_digest": _LEASE_QUEUE,
            "model_digest": _LEASE_QUEUE,
            "max_attempts": _MAX_LEASE_ATTEMPTS,
            "deadline_unix": None,
            "metadata": metadata,
            "provenance_refs": [],
            "max_tenant_in_flight": _MAX_TENANT_IN_FLIGHT,
        }
    )


# The lease is intentionally short enough to make a crashed poller fail over in
# bounded time, while the renewal loop keeps a healthy long-poll owner alive.
DEFAULT_LEASE_TTL_S = 90.0
_RENEW_INTERVAL_MIN_S = 0.01
_RENEW_INTERVAL_MAX_S = 30.0
_LEASE_KIND = "messaging_intake_lease"
_LEASE_QUEUE = "messaging-intake"
_LEASE_RESOURCE_CLASS = "messaging-intake"
# A lease generation is not a completed unit of work.  Keep the durable row
# reclaimable for the life of the deployment instead of dead-lettering it after
# the generic WorkItem default of three attempts.
_MAX_LEASE_ATTEMPTS = 2**31 - 1
_MAX_TENANT_IN_FLIGHT = 4096


class IntakeLeaseState(enum.Enum):
    """The closed states of one intake lease (ARBORA-MESSAGING-R800.1003)."""

    HELD = "held"
    SUSPECT = "suspect"
    LOST = "lost"
    RELEASED = "released"


class IntakeLeaseEvent(enum.Enum):
    """The closed events of the intake lease machine."""

    HEARTBEAT_OK = "heartbeat_ok"
    HEARTBEAT_ERR = "heartbeat_err"
    FENCE_REFUSED = "fence_refused"
    TTL_LAPSED = "ttl_lapsed"
    REACQUIRED = "reacquired"
    RELEASE = "release"


_S = IntakeLeaseState
_E = IntakeLeaseEvent
#: A lease starts held: it exists only once the engine granted the claim.
INITIAL = _S.HELD
#: A released lease is handed back for good; a later acquire builds a new ``IntakeLease``.
TERMINALS = (_S.RELEASED,)
#: The 12 legal edges. Every other (state, event) pair is refused.
_LEASE_TRANSITIONS: Mapping[
    tuple[IntakeLeaseState, IntakeLeaseEvent], IntakeLeaseState
] = MappingProxyType(
    {
        (_S.HELD, _E.HEARTBEAT_OK): _S.HELD,
        (_S.HELD, _E.HEARTBEAT_ERR): _S.SUSPECT,
        (_S.SUSPECT, _E.HEARTBEAT_OK): _S.HELD,
        (_S.SUSPECT, _E.HEARTBEAT_ERR): _S.SUSPECT,  # guarded by within_ttl
        (_S.HELD, _E.FENCE_REFUSED): _S.LOST,
        (_S.SUSPECT, _E.FENCE_REFUSED): _S.LOST,
        (_S.HELD, _E.TTL_LAPSED): _S.LOST,
        (_S.SUSPECT, _E.TTL_LAPSED): _S.LOST,
        (_S.LOST, _E.REACQUIRED): _S.HELD,
        (_S.HELD, _E.RELEASE): _S.RELEASED,
        (_S.SUSPECT, _E.RELEASE): _S.RELEASED,
        (_S.LOST, _E.RELEASE): _S.RELEASED,
    }
)


class IllegalIntakeLeaseTransition(Exception):
    """A lease event that the machine refuses (or whose guard failed)."""

    code = "INTAKE_LEASE_TRANSITION_ILLEGAL"
    machine = "messaging_intake_lease"

    def __init__(self, from_state: IntakeLeaseState, event: IntakeLeaseEvent) -> None:
        self.from_state = from_state
        self.event = event
        super().__init__(
            f"{self.code}: machine={self.machine} from_state={from_state.value} "
            f"event={event.value}"
        )


@dataclasses.dataclass
class IntakeLease:
    """One engine-issued claim authorizing a platform's inbound poller.

    ``state`` is written only by :func:`transition`; the engine WorkItem row
    stays the durable owner record.
    """

    platform: str
    item_id: str
    claim: dict[str, Any]
    lease_ttl_s: float
    state: IntakeLeaseState = INITIAL
    last_good_s: float = dataclasses.field(default_factory=lambda: _clock())


def transition(
    lease: IntakeLease, event: IntakeLeaseEvent, *, within_ttl: bool = True
) -> IntakeLeaseState:
    """Apply ``event`` to ``lease``; the one writer of ``IntakeLease.state``."""
    target = _LEASE_TRANSITIONS.get((lease.state, event))
    guard_failed = (
        lease.state is _S.SUSPECT and event is _E.HEARTBEAT_ERR and not within_ttl
    )
    if target is None or guard_failed:
        raise IllegalIntakeLeaseTransition(lease.state, event)
    lease.state = target
    return target


def _session_tenant(session: Any) -> str:
    tenant = str(getattr(session, "tenant", "") or "").strip()
    if not tenant:
        raise NativeWorkItemRequired(
            "messaging intake lease requires a verified non-empty session tenant"
        )
    return tenant


def _identity_digest(platform: str) -> str:
    """Return a stable, non-secret identity reference for one backend config."""
    from arbora.messaging.service import MessagingService

    return MessagingService.instance().identity_digest(platform)


def _work_item_id(platform: str, identity_digest: str) -> str:
    key = f"messaging-intake:{platform}:{identity_digest}"
    return f"workitem:messaging-intake:{hashlib.sha256(key.encode()).hexdigest()}"


def _validate_lease_row(
    engine: Any,
    *,
    item_id: str,
    platform: str,
    identity_digest: str,
    tenant: str,
) -> None:
    item = _work_items(engine).get(tenant=tenant, work_item_id=item_id)
    metadata = item.get("metadata") if item is not None else None
    if (
        item is None
        or item.get("kind") != _LEASE_KIND
        or not isinstance(metadata, dict)
        or metadata.get("platform") != platform
        or metadata.get("identity_digest") != identity_digest
    ):
        # Never claim a row whose immutable identity does not match this
        # platform/configuration.  A collision or operator-created row fails
        # closed instead of becoming an alternate ownership authority.
        raise WorkItemBackendUnavailable(
            f"messaging intake lease row {item_id!r} failed identity validation"
        )


def acquire_intake_lease(
    engine: Any,
    platform: str,
    session: Any,
    *,
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
) -> IntakeLease | None:
    """Submit and atomically claim the one durable lease for ``platform``.

    ``None`` is an authoritative non-owner result from the engine.  Missing
    native WorkItem capabilities raise so the caller can log a fail-closed
    deployment error rather than accidentally starting an un-fenced poller.
    """
    if lease_ttl_s <= 0:
        raise ValueError("lease_ttl_s must be positive")
    platform = str(platform).strip()
    if not platform:
        raise ValueError("platform must be non-empty")

    tenant = _session_tenant(session)
    identity_digest = _identity_digest(platform)
    item_id = _work_item_id(platform, identity_digest)
    _submit_lease_row(
        engine,
        item_id=item_id,
        tenant=tenant,
        metadata={"platform": platform, "identity_digest": identity_digest},
    )
    _validate_lease_row(
        engine,
        item_id=item_id,
        platform=platform,
        identity_digest=identity_digest,
        tenant=tenant,
    )

    answer, owner_ref = _claim(engine, platform, item_id, tenant, lease_ttl_s)
    if not answer.get("claimed"):
        logger.info(
            "messaging intake lease is held by another owner: platform=%s", platform
        )
        return None
    claim = {**answer, "tenant": tenant, "worker_ref": owner_ref}
    return IntakeLease(
        platform=platform,
        item_id=item_id,
        claim=claim,
        lease_ttl_s=lease_ttl_s,
    )


def _claim(
    engine: Any, platform: str, item_id: str, tenant: str, lease_ttl_s: float
) -> tuple[dict[str, Any], str]:
    """The one engine claim call, shared by first acquire and re-acquire."""
    owner_ref = f"messaging-intake:{platform}:{uuid.uuid4().hex}"
    answer = _work_items(engine).claim(
        {
            "schema_version": "1",
            "tenant_ref": tenant,
            "work_item_id": item_id,
            "queue_ref": None,
            "resource_class": _LEASE_RESOURCE_CLASS,
            "fairness_group": f"messaging:{platform}",
            "worker_ref": owner_ref,
            "now_ms": _now_ms(),
            "lease_ms": int(lease_ttl_s * 1000),
            "max_tenant_in_flight": _MAX_TENANT_IN_FLIGHT,
        }
    )
    return answer, owner_ref


def acquire_intake_leases(
    engine: Any,
    platforms: Iterable[str],
    session: Any,
    *,
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
) -> tuple[IntakeLease, ...]:
    """Acquire every platform lease that this process may safely poll.

    A platform with no native lease capability or a live competing owner is
    omitted.  The caller must pass only the returned platforms to the listener;
    it must never fall back to the configured list.
    """
    leases: list[IntakeLease] = []
    for platform in dict.fromkeys(str(value).strip() for value in platforms):
        if not platform:
            continue
        try:
            lease = acquire_intake_lease(
                engine,
                platform,
                session,
                lease_ttl_s=lease_ttl_s,
            )
        except Exception as exc:  # intake must fail closed on any lease error
            logger.error(
                "messaging intake lease unavailable: platform=%s error=%s; "
                "refusing inbound polling",
                platform,
                exc,
                exc_info=True,
            )
            continue
        if lease is not None:
            leases.append(lease)
    return tuple(leases)


def _release_intake_lease(engine: Any, lease: IntakeLease) -> None:
    """Return a healthy lease to ``ready``; expiry remains the crash fallback."""
    now_ms = _now_ms()
    transition(lease, IntakeLeaseEvent.RELEASE)
    try:
        _work_items(engine).defer(
            tenant=lease.claim["tenant"],
            work_item_id=lease.item_id,
            worker_id=lease.claim["worker_ref"],
            lease_epoch=int(lease.claim["lease_epoch"]),
            fencing_token=int(lease.claim["fencing_token"]),
            idempotency_key=f"defer:{lease.item_id}:{lease.claim['lease_epoch']}",
            next_retry_at_ms=now_ms,
            now_ms=now_ms,
            reason_ref="messaging_intake_stopped",
        )
    except Exception as exc:  # expiry is the durable crash fallback
        logger.debug(
            "messaging intake lease release deferred to expiry: platform=%s error=%s",
            lease.platform,
            exc,
            exc_info=True,
        )


def _renew_interval(leases: tuple[IntakeLease, ...]) -> float:
    """How often to re-heartbeat, bounded between the floor and the ceiling."""
    return min(
        _RENEW_INTERVAL_MAX_S,
        max(_RENEW_INTERVAL_MIN_S, min(lease.lease_ttl_s for lease in leases) / 3),
    )


def _still_leased(
    leases: tuple[IntakeLease, ...],
    platform_stop_events: dict[str, threading.Event],
) -> list[IntakeLease]:
    """Leases whose platform has not already been dropped from polling."""
    return [
        lease for lease in leases if not platform_stop_events[lease.platform].is_set()
    ]


def _renew_one(engine: Any, lease: IntakeLease) -> IntakeLeaseEvent:
    """Heartbeat one lease and report what happened as a machine event.

    An engine refusal is ``fence_refused``; an exception is only a
    ``heartbeat_err``, which leaves the platform polling (ARBORA-MESSAGING-R800.1004).
    """
    try:
        answer = _work_items(engine).renew(
            tenant=lease.claim["tenant"],
            work_item_id=lease.item_id,
            worker_id=lease.claim["worker_ref"],
            lease_epoch=int(lease.claim["lease_epoch"]),
            fencing_token=int(lease.claim["fencing_token"]),
            now_ms=_now_ms(),
            lease_ms=int(lease.lease_ttl_s * 1000),
        )
    except Exception as exc:  # a transient heartbeat error is not a loss
        logger.error(
            "messaging intake lease renewal failed: platform=%s error=%s",
            lease.platform,
            type(exc).__name__,
            exc_info=True,
        )
        return IntakeLeaseEvent.HEARTBEAT_ERR
    if bool(answer.get("renewed", answer.get("ok", False))):
        return IntakeLeaseEvent.HEARTBEAT_OK
    return IntakeLeaseEvent.FENCE_REFUSED


def _try_reacquire(engine: Any, lease: IntakeLease) -> bool:
    """Claim a lost lease again through the same engine call as the first acquire."""
    try:
        answer, owner_ref = _claim(
            engine,
            lease.platform,
            lease.item_id,
            lease.claim["tenant"],
            lease.lease_ttl_s,
        )
    except Exception as exc:  # the engine is unreachable: stay lost
        logger.error(
            "messaging intake re-acquire failed: platform=%s error=%s",
            lease.platform,
            type(exc).__name__,
            exc_info=True,
        )
        return False
    if not answer.get("claimed"):
        return False
    lease.claim = {**answer, "tenant": lease.claim["tenant"], "worker_ref": owner_ref}
    lease.last_good_s = _clock()
    return True


def _apply_heartbeat(engine: Any, lease: IntakeLease) -> IntakeLeaseState:
    """Heartbeat once and move the lease machine; returns the new state."""
    event = _renew_one(engine, lease)
    now = _clock()
    within_ttl = now - lease.last_good_s < lease.lease_ttl_s
    if event is IntakeLeaseEvent.HEARTBEAT_ERR and not within_ttl:
        event = IntakeLeaseEvent.TTL_LAPSED
    state = transition(lease, event, within_ttl=within_ttl)
    if event is IntakeLeaseEvent.HEARTBEAT_OK:
        lease.last_good_s = now
    return state


def _handle_lease_loss(
    lease: IntakeLease,
    leases: tuple[IntakeLease, ...],
    platform_stop_events: dict[str, threading.Event],
    state_lock: threading.Lock,
    stop_event: threading.Event,
) -> None:
    """Drop one platform's polling; stop the whole intake once none remain leased."""
    with state_lock:
        platform_stop_events[lease.platform].set()
        remaining = _still_leased(leases, platform_stop_events)
    if remaining:
        logger.error(
            "messaging intake lease lost: platform=%s; dropping it "
            "from inbound polling, other platforms continue",
            lease.platform,
        )
        return
    logger.error(
        "messaging intake lease lost: platform=%s; no platform "
        "holds a lease, stopping inbound polling",
        lease.platform,
    )
    stop_event.set()


def run_with_intake_leases(
    engine: Any,
    leases: tuple[IntakeLease, ...],
    stop_event: threading.Event,
    serve: Callable[[list[str], threading.Event, dict[str, threading.Event]], None],
) -> None:
    """Renew ``leases`` while ``serve`` owns the corresponding pollers.

    ``serve`` is called exactly once, up front, with every leased platform AND
    a per-platform ``threading.Event`` map (``platform_stop_events``). When one
    platform's lease is lost, this function sets ONLY that platform's event —
    ``serve``'s implementation (`arbora.messaging.polling.run_poll_loop`)
    is responsible for tearing down just that one platform's listener without
    touching the others. The shared ``stop_event`` is reserved for a genuine
    full stop: an external shutdown request (already set by the caller, e.g.
    SIGTERM) or every platform having lost its lease.

    Previously any single platform's renewal failure set the one shared
    ``stop_event`` unconditionally, so — since the daemon ran every backend's
    listener under ONE task cancelled off that one event — one platform's
    lease loss silently killed inbound polling for every OTHER healthy
    platform too.
    """
    if not leases:
        stop_event.set()
        return

    state_lock = threading.Lock()
    platform_stop_events = {lease.platform: threading.Event() for lease in leases}
    renew_stop = threading.Event()

    def _renew() -> None:
        interval = _renew_interval(leases)
        while not renew_stop.wait(interval):
            if stop_event.is_set():
                return
            still_leased = _still_leased(leases, platform_stop_events)
            if not still_leased:
                return
            for lease in still_leased:
                if _apply_heartbeat(engine, lease) is not IntakeLeaseState.LOST:
                    continue
                # A lost lease may be won again without a restart; only a
                # failed re-acquire drops the platform's listener.
                if _try_reacquire(engine, lease):
                    transition(lease, IntakeLeaseEvent.REACQUIRED)
                    continue
                _handle_lease_loss(
                    lease, leases, platform_stop_events, state_lock, stop_event
                )

    # threading.Thread does NOT inherit contextvars (unlike asyncio.Task), so
    # the renewal thread would otherwise run with an EMPTY context — losing
    # the ambient GraphSession the caller entered via use_session() before
    # starting this run, even though the initial lease *acquisition* on the
    # calling thread worked fine. copy_context() carries that session into
    # the worker thread so heartbeat()'s engine calls stay authorized instead
    # of raising SessionRequiredError on the first renewal.
    renew_ctx = contextvars.copy_context()
    renew_thread = threading.Thread(
        target=lambda: renew_ctx.run(_renew),
        daemon=True,
        name="MessagingIntakeLeaseRenewal",
    )
    renew_thread.start()
    try:
        serve([lease.platform for lease in leases], stop_event, platform_stop_events)
    finally:
        renew_stop.set()
        renew_thread.join(
            timeout=max(
                1.0,
                min(_RENEW_INTERVAL_MAX_S, min(lease.lease_ttl_s for lease in leases)),
            )
        )
        for lease in leases:
            _release_intake_lease(engine, lease)


def run_owned_intake(
    engine: Any,
    platforms: Iterable[str],
    session: Any,
    stop_event: threading.Event,
    serve: Callable[[list[str], threading.Event, dict[str, threading.Event]], None],
    *,
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
) -> None:
    """Run ``serve`` only after native leases admit this caller.

    This is the one synchronous boundary shared by the standalone messaging
    daemon and the embedded arbora co-service. Callers must provide the
    deployment's explicit, verified session; credentials/platform discovery
    alone never reaches ``serve``. A missing lease (including an unavailable
    native WorkItem backend) stops the caller without opening a poller.

    ``serve`` receives ``(platforms, stop_event, platform_stop_events)`` — the
    third argument is a per-platform ``threading.Event`` map so a single
    platform's lease loss can be handled without stopping the others (see
    :func:`run_with_intake_leases`).
    """
    leases = acquire_intake_leases(
        engine,
        platforms,
        session,
        lease_ttl_s=lease_ttl_s,
    )
    if not leases:
        logger.error("messaging inbound intake has no native lease; refusing to poll")
        stop_event.set()
        return
    run_with_intake_leases(engine, leases, stop_event, serve)


__all__ = [
    "DEFAULT_LEASE_TTL_S",
    "NativeWorkItemRequired",
    "WorkItemBackendUnavailable",
    "IllegalIntakeLeaseTransition",
    "IntakeLease",
    "IntakeLeaseEvent",
    "IntakeLeaseState",
    "transition",
    "acquire_intake_lease",
    "acquire_intake_leases",
    "run_owned_intake",
    "run_with_intake_leases",
]
