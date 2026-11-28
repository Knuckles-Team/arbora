"""The onboarding service, the handshake onboarder and the served catalog digest (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import threading
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from arbora.access.action_policy import ActionPolicies
from arbora.core.environment import read_env
from arbora.fleet.declarations import (
    RESOURCES_URI,
    Declarations,
)
from arbora.fleet.onboarding import (
    FleetOnboarding,
    OnboardingReport,
    OnboardingTrigger,
    TriggerReason,
    _failure,
    onboard_fleet,
    refresh_serving_catalog,
)
from arbora.fleet.onboarding_capture import (
    FleetEndpoint,
    _child_auth,
    configured_fleet_endpoints,
    self_served_connectors,
)
from arbora.fleet.onboarding_machine import (
    OnboardingEvent,
    OnboardingState,
    transition,
)
from arbora.fleet.release_gate import install_release_gate
from arbora.fleet.release_record import ReleaseRecorder
from arbora.fleet.smoke_sweep import FleetSmokeService
from arbora.security.error_surface import carry_errors

logger = logging.getLogger(__name__)


#: How often the boot thread re-runs a pass after the first.
PASS_INTERVAL_S = 1800.0


#: A burst of handshakes for one connector inside this window is one pass.
HANDSHAKE_DEBOUNCE_S = 2.0


#: Retry delays double from the base up to the cap, for at most this many tries.
RETRY_BASE_S = 5.0


RETRY_CAP_S = 300.0


RETRY_MAX_ATTEMPTS = 6


#: The slow-retry period out of ``failed``, once the process lease is Live (R080.2).
SLOW_RETRY_S = 60.0


#: The states in which a new handshake joins the worker that is already running.
_JOINABLE = frozenset({OnboardingState.DEBOUNCING, OnboardingState.RUNNING})


#: The engine code that holds a large pack shrink for operator approval.
MASS_WITHDRAWAL_CODE = "PACK_MASS_WITHDRAWAL"


@dataclass(slots=True)
class FleetOnboardingService:
    """The boot-time background pass and its periodic renewal."""

    onboarding: FleetOnboarding
    multiplexer: Any
    served_url: str = ""
    interval_s: float = PASS_INTERVAL_S
    stop: threading.Event = field(default_factory=threading.Event)
    #: The scheduled smoke sweep, ticked beside each pass (ARBORA-FLEET-R053).
    smoke: FleetSmokeService | None = None

    def endpoints(self) -> tuple[FleetEndpoint, ...]:
        exclude = {*self_served_connectors(), *self._skipped()}
        return configured_fleet_endpoints(exclude=exclude)

    def _skipped(self) -> Collection[str]:
        return getattr(self.multiplexer, "_skip_servers", None) or ()

    async def run_pass(self) -> OnboardingReport:
        endpoints = self.endpoints()
        report = await onboard_fleet(
            self.onboarding,
            endpoints,
            served_url=self.served_url,
            admitted=set(self.multiplexer.load_catalog()),
        )
        fleet = [item.name for item in endpoints]
        await refresh_serving_catalog(self.multiplexer, report, fleet)
        self.multiplexer._fleet_onboarding = merge_handshake_records(
            report.as_dict(), self.onboarding.handshake
        )
        logger.info("fleet onboarding pass: %s", report.as_dict())
        return report

    def passes(self) -> Iterator[None]:
        """Yield before each pass: one now, then one per interval until stopped."""

        yield
        while not self.stop.wait(self.interval_s):
            yield

    def run_forever(self) -> None:
        from arbora.security.actor_context import use_actor
        from arbora.security.session import use_session

        session = self.onboarding.session
        with use_actor(session.actor), use_session(session):
            for _ in self.passes():
                self._run_pass_safely()

    def _run_pass_safely(self) -> None:
        try:
            asyncio.run(self._run_pass_scoped())
        except Exception as exc:  # serving must outlive any onboarding fault
            logger.warning("fleet onboarding pass failed: %s", exc, exc_info=True)
        if self.smoke is not None:
            try:
                asyncio.run(self.smoke.tick())
            except Exception as exc:  # a failed sweep never stops onboarding
                logger.warning("fleet smoke sweep failed: %s", exc, exc_info=True)

    async def _run_pass_scoped(self) -> OnboardingReport:
        from arbora.epistemic import temporary_client_scope

        # The pass runs on a short-lived loop in this worker thread. Its EG
        # transports are scoped to it, so they never displace the serving
        # loop's pool clients or outlive the loop (ARBORA-FLEET-R041.18).
        async with temporary_client_scope():
            return await self.run_pass()

    def start(self) -> threading.Thread:
        thread = threading.Thread(
            target=carry_errors(self.run_forever), name="fleet-onboarding", daemon=True
        )
        thread.start()
        return thread


def merge_handshake_records(
    snapshot: dict[str, Any], records: Mapping[str, dict[str, Any]]
) -> dict[str, Any]:
    """Add each connector's handshake record to its R040 surface entry."""

    surfaces = dict(snapshot.get("surfaces", {}))
    for name, record in records.items():
        surfaces[name] = {**surfaces.get(name, {}), "onboarding": dict(record)}
    return {**snapshot, "surfaces": surfaces}


def _digest_rows(rows: Any) -> str:
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, default=str).encode()
    ).hexdigest()


def handshake_catalog_digest(
    tools: Sequence[Any],
    prompts: Sequence[Any] = (),
    resources: Sequence[Any] = (),
    declaration: str = "",
    failures: Sequence[str] = (),
) -> str:
    """A content digest of the served lists and the data-source declaration.

    Covers each tool (name and input schema), prompt (name, description,
    arguments, tags, meta) and resource (uri, mime type) the server lists, plus
    the body of ``connector://resources``. The ``serverInfo`` version is not an
    input: a release that changes nothing served changes nothing here.
    """

    def dump(item: Any) -> Any:
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json", by_alias=True, exclude_none=True)
        return str(item)

    return _digest_rows(
        {
            "tools": sorted(
                (
                    str(getattr(t, "name", "")),
                    getattr(t, "inputSchema", None),
                    getattr(t, "meta", None) or getattr(t, "_meta", None),
                )
                for t in tools
            ),
            "prompts": sorted(_digest_rows(dump(p)) for p in prompts),
            "resources": sorted(_digest_rows(dump(r)) for r in resources),
            "declaration": declaration,
            # A failed optional discovery call is distinct from an empty
            # surface.  Keep only bounded exception type markers: the digest
            # must never carry transport text, credentials, or endpoint data.
            "failures": sorted(set(str(marker)[:80] for marker in failures)),
        }
    )


async def served_catalog_digest(session: Any, tools: Sequence[Any]) -> str:
    """Digest the lists and resources a live session serves (R041.8).

    A list or read the server does not support still counts as an empty
    surface, while its failed discovery is retained in the content digest so
    a transient failure cannot masquerade as an unchanged empty surface.
    """

    failures: list[str] = []

    async def optional(call: Any, attr: str, surface: str) -> list[Any]:
        try:
            return list(getattr(await call(), attr))
        except Exception:
            logger.debug("optional failed", exc_info=True)
            # Do not let a transient discovery/read error masquerade as an
            # empty, unchanged surface.  The next handshake must produce a
            # different digest and drive the existing recapture path.
            failures.append(f"{surface}:discovery_failed")
            return []

    prompts = await optional(session.list_prompts, "prompts", "prompts")
    resources = await optional(session.list_resources, "resources", "resources")
    declaration = ""
    if any(str(getattr(r, "uri", "")) == RESOURCES_URI for r in resources):
        try:
            result = await session.read_resource(RESOURCES_URI)
            declaration = "".join(
                str(getattr(c, "text", "") or "") for c in result.contents
            )
        except Exception:
            logger.warning("served_catalog_digest failed", exc_info=True)
            failures.append("declaration:read_failed")
            declaration = ""
    return handshake_catalog_digest(
        tools, prompts, resources, declaration, failures=failures
    )


@dataclass(slots=True)
class _Handshake:
    seq: int
    server_version: str
    digest: str
    reason: TriggerReason = "handshake"


@dataclass(slots=True)
class HandshakeOnboarder:
    """Schedules one onboarding pass per completed connector handshake.

    ``notify`` is called by the multiplexer after ``initialize`` and the list
    calls. It only records the handshake and starts (or joins) a worker
    thread, so it never blocks the session or tool serving (R041.7). A burst
    of handshakes for one connector coalesces into one pass. A failed pass is
    logged, recorded per connector, shown in the surface report and retried
    with bounded exponential backoff that resets on success (R041.9). A pass
    held for the mass-withdrawal approval is not retried (R041.10).
    """

    onboarding: FleetOnboarding
    multiplexer: Any
    endpoints: Callable[[], Sequence[FleetEndpoint]]
    debounce_s: float = HANDSHAKE_DEBOUNCE_S
    retry_base_s: float = RETRY_BASE_S
    retry_cap_s: float = RETRY_CAP_S
    max_attempts: int = RETRY_MAX_ATTEMPTS
    stop: threading.Event = field(default_factory=threading.Event)
    #: Waits ``seconds`` and returns True when stopped; replaced in tests.
    wait: Callable[[float], bool] | None = None
    passes_run: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _latest: dict[str, _Handshake] = field(default_factory=dict)
    _active: dict[str, threading.Thread] = field(default_factory=dict)
    _seq: int = 0
    _state_lock: threading.RLock = field(default_factory=threading.RLock)

    def notify(
        self,
        connector: str,
        *,
        server_version: str,
        digest: str,
        reason: TriggerReason = "handshake",
    ) -> None:
        """Record a completed handshake and schedule one pass; never raises."""

        try:
            with self._lock:
                self._seq += 1
                info = _Handshake(self._seq, server_version, digest, reason)
                self._latest[connector] = info
                if connector in self._active:
                    # Joined to the running worker: Debouncing coalesces and
                    # Running joins; Backoff keeps waiting and uses ``info``.
                    if self._state(connector) in _JOINABLE:
                        self._record(connector, info, OnboardingEvent.REGISTERED, 0, "")
                    return
                state = self._state(connector)
                if state is OnboardingState.IDLE:
                    self._record(connector, info, OnboardingEvent.REGISTERED, 0, "")
                elif state is OnboardingState.AWAITING_APPROVAL:
                    # A new handshake after the operator's decision is the
                    # approval: the pass runs again and is held again if it
                    # still is not approved.
                    self._record(connector, info, OnboardingEvent.APPROVED, 0, "")
                elif state is OnboardingState.FAILED:
                    if self._lease_live():
                        self._record(
                            connector, info, OnboardingEvent.AUTHORITY_LIVE, 0, ""
                        )
                        self._record(
                            connector, info, OnboardingEvent.RETRY_ELAPSED, 0, ""
                        )
                    else:
                        return  # the slow retry waits for the live lease
                thread = threading.Thread(
                    target=carry_errors(self._work),
                    args=(connector,),
                    name=f"handshake-onboard-{connector}",
                    daemon=True,
                )
                self._active[connector] = thread
            thread.start()
        except Exception as exc:  # serving must outlive any onboarding fault
            logger.warning(
                "handshake onboarding of %s not scheduled: %s",
                connector,
                exc,
                exc_info=True,
            )

    def drain(self, timeout: float = 10.0) -> None:
        """Join every running worker; for shutdown and tests."""

        for thread in list(self._active.values()):
            thread.join(timeout)

    def _sleep(self, seconds: float) -> bool:
        return (self.wait or self.stop.wait)(seconds)

    def _state(self, connector: str) -> OnboardingState:
        raw = self.onboarding.handshake.get(connector, {}).get("state", "idle")
        try:
            return OnboardingState(raw)
        except ValueError:
            return OnboardingState.IDLE

    def _lease_live(self) -> bool:
        """True while the process credential lease is Live (ARBORA-IDENTITY-R027.1)."""

        from arbora.security.actor_context import LeaseState

        actor = getattr(self.onboarding.session, "actor", None)
        lease = getattr(actor, "credential_lease", None)
        state = getattr(lease, "state", None)
        return lease is None or state is LeaseState.LIVE

    def _work(self, connector: str) -> None:
        try:
            self._loop(connector)
        except Exception as exc:
            logger.warning(
                "handshake onboarding of %s crashed: %s", connector, exc, exc_info=True
            )
        finally:
            with self._lock:
                self._active.pop(connector, None)

    def _info(self, connector: str) -> _Handshake:
        with self._lock:
            return self._latest[connector]

    def _loop(self, connector: str) -> None:
        if self._state(connector) is OnboardingState.DEBOUNCING:
            if self._sleep(self.debounce_s):
                return
            self._record(
                connector,
                self._info(connector),
                OnboardingEvent.DEBOUNCE_ELAPSED,
                0,
                "",
            )
        attempts: int | None = 0
        while attempts is not None:
            attempts = self._step(connector, attempts)

    def _step(self, connector: str, attempts: int) -> int | None:
        """One pass of the loop: the attempts to carry on with, or ``None`` when done."""
        info = self._info(connector)
        endpoint = next((e for e in self.endpoints() if e.name == connector), None)
        if endpoint is None:
            return None  # not a fleet endpoint: nothing to onboard
        status, error = self._attempt(endpoint, info)
        self.passes_run += 1
        if status == "ok":
            return self._after_ok(connector, info, error)
        attempts += 1
        if status == "held":
            self._record(connector, info, OnboardingEvent.HELD, attempts, error)
            return None
        return self._after_error(connector, info, error, attempts)

    def _after_ok(self, connector: str, info: _Handshake, outcome: str) -> int | None:
        """A pass succeeded: finish, or debounce and run again after a newer handshake."""
        self._record(connector, info, OnboardingEvent.PASS_OK, 0, "", outcome=outcome)
        with self._lock:
            if self._latest[connector].seq == info.seq:
                self._active.pop(connector, None)
                return None
        # A newer handshake arrived mid-pass: debounce and run again.
        newest = self._info(connector)
        self._record(connector, newest, OnboardingEvent.REGISTERED, 0, "")
        if self._sleep(self.debounce_s):
            return None
        self._record(connector, newest, OnboardingEvent.DEBOUNCE_ELAPSED, 0, "")
        return 0

    def _after_error(
        self, connector: str, info: _Handshake, error: str, attempts: int
    ) -> int | None:
        """A pass failed: back off and retry, or wait out a dead lease from ``failed``."""
        delay = min(self.retry_cap_s, self.retry_base_s * 2 ** (attempts - 1))
        self._record(connector, info, OnboardingEvent.PASS_ERR, attempts, error, delay)
        if self._state(connector) is OnboardingState.FAILED:
            return 0 if self._await_live_lease(connector, info) else None
        if self._sleep(delay):
            return None
        self._record(connector, info, OnboardingEvent.RETRY_ELAPSED, attempts, "")
        return attempts

    def _await_live_lease(self, connector: str, info: _Handshake) -> bool:
        """The slow-retry edge out of ``failed``: wait for a Live lease, then retry."""

        while True:
            period = SLOW_RETRY_S * (1 + 0.2 * random.random())  # nosec B311 - jitter only, not security
            if self._sleep(period):
                return False
            if self._lease_live():
                self._record(connector, info, OnboardingEvent.AUTHORITY_LIVE, 0, "")
                self._record(connector, info, OnboardingEvent.RETRY_ELAPSED, 0, "")
                return True

    def _attempt(self, endpoint: FleetEndpoint, info: _Handshake) -> tuple[str, str]:
        """Run one pass; return ``("ok", outcome)``, ``("held", e)`` or ``("error", e)``."""

        from arbora.security.actor_context import use_actor
        from arbora.security.session import use_session

        session = self.onboarding.session
        trigger = OnboardingTrigger(endpoint.name, info.reason, info.digest)
        try:
            with use_actor(session.actor), use_session(session):
                outcome = asyncio.run(self._pass(endpoint, info, trigger))
        except Exception as exc:
            if getattr(
                exc, "code", None
            ) == MASS_WITHDRAWAL_CODE or MASS_WITHDRAWAL_CODE in str(exc):
                logger.warning(
                    "%s: handshake pass awaits mass-withdrawal approval", endpoint.name
                )
                return "held", _failure(exc)
            logger.warning(
                "%s: handshake onboarding failed: %s", endpoint.name, exc, exc_info=True
            )
            return "error", _failure(exc)
        return "ok", outcome

    async def _pass(
        self, endpoint: FleetEndpoint, info: _Handshake, trigger: OnboardingTrigger
    ) -> str:
        from arbora.epistemic import temporary_client_scope

        # The pass runs on a fresh ``asyncio.run`` loop in this worker thread,
        # not the serving loop. Its EG transports are scoped to that loop, so
        # a pack-changing handshake never displaces, or fails on, the serving
        # loop's live pool clients (ARBORA-FLEET-R041.18).
        async with temporary_client_scope():
            return await self.onboarding.handshake_pass(
                endpoint,
                auth=_child_auth(self.onboarding.config),
                trigger=trigger,
                server_version=info.server_version,
            )

    def _record(
        self,
        connector: str,
        info: _Handshake,
        event: OnboardingEvent,
        attempts: int,
        error: str,
        retry_in_s: float | None = None,
        *,
        outcome: str = "",
    ) -> None:
        """The only caller of ``transition``: apply ``event`` and record the result."""

        with self._state_lock:
            state = transition(
                self._state(connector),
                event,
                attempts_left=attempts < self.max_attempts,
                lease_live=self._lease_live(),
            )
            record: dict[str, Any] = {
                "reason": info.reason,
                "state": state.value,
                "attempts": attempts,
                "server_version": info.server_version,
                "catalog_digest": info.digest,
            }
            if outcome:
                record["outcome"] = outcome
            if error:
                record["error"] = error
            if retry_in_s is not None:
                record["retry_in_s"] = retry_in_s
            self.onboarding.handshake[connector] = record
            current = self.multiplexer._fleet_onboarding
            self.multiplexer._fleet_onboarding = merge_handshake_records(
                dict(current or {}), {connector: record}
            )


def configured_served_url() -> str:
    """The MCP URL Arbora serves at, from ``ARBORA_SERVED_MCP_URL``."""

    return str(read_env("ARBORA_SERVED_MCP_URL") or "")


def start_fleet_onboarding(
    *,
    gateway: Any,
    engine: Any,
    session: Any,
    multiplexer: Any,
    virtual_catalog: Any,
    declarations: Declarations,
    action_policies: ActionPolicies,
    served_url: str | None = None,
    smoke_service: FleetSmokeService | None = None,
    spawn_worker: Callable[[tuple[str, ...]], Any] | None = None,
    feature_schema: Callable[[str], Mapping[str, Any]] | None = None,
) -> threading.Thread | None:
    """Start the background onboarding service; never raise into boot.

    ``served_url`` defaults to :func:`configured_served_url`. Without it, the
    self-served registrations renew at their live URL.
    """

    if getattr(engine, "graph_compute", None) is None or multiplexer is None:
        logger.warning("fleet onboarding disabled: no engine or multiplexer")
        return None
    bind_scope = getattr(virtual_catalog, "bind_scope", None)
    if callable(bind_scope):
        bind_scope(str(session.tenant), str(session.graph))
    from arbora.fleet.host_composition import (
        compose_onboarding_ports,
        declare_data_prep_at_boot,
    )

    ports = compose_onboarding_ports(
        gateway, engine, session, spawn=spawn_worker, feature_schema=feature_schema
    )
    declare_data_prep_at_boot(engine, session)
    service = FleetOnboardingService(
        onboarding=FleetOnboarding(
            engine=engine,
            session=session,
            declarations=declarations,
            action_policies=action_policies,
            virtual_catalog=virtual_catalog,
            definition_admission=ports.definition_admission,
            config=multiplexer.fleet.config,
            release_recorder=ReleaseRecorder(),
        ),
        multiplexer=multiplexer,
        served_url=configured_served_url() if served_url is None else served_url,
        smoke=smoke_service,
    )
    onboarder = HandshakeOnboarder(
        onboarding=service.onboarding,
        multiplexer=multiplexer,
        endpoints=service.endpoints,
    )
    setter = getattr(multiplexer, "set_handshake_listener", None)
    if setter is not None:
        setter(onboarder.notify)
    # Every child launch and attach passes the one release gate (ARBORA-HOST-R037.3).
    install_release_gate(service.onboarding.release_gate)
    return service.start()
