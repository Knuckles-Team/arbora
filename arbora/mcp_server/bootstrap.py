"""Engine authority, materialization, and worker lifecycle for arbora."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, cast

from arbora.security.anonymous import session_context

logger = logging.getLogger(__name__)

# Bound by ``runtime`` after this module's definitions load. Keeping the host
# state injection explicit avoids an eager bootstrap↔runtime import cycle.
_AGENT_ID = cast(Any, None)
_AUTHORITY_KEEPALIVE_ACTIVE = cast(Any, None)
_ENGINE_LOCK = cast(Any, None)
_PROCESS_AUTHORITY_STOP = cast(Any, None)
_PROCESS_AUTHORITY_THREAD = cast(Any, None)
_PROCESS_SESSION_REFRESH_LOCK = cast(Any, None)
_SESSION_ID = cast(Any, None)
setting = cast(Any, None)


def _get_engine():
    """The process engine handle over the one served EG client pool.

    The same :func:`~arbora.mcp_server.served_engine.process_engine_pool`
    backs the operation-invoke API, so startup composition and served
    operations share one set of EG transports. Every request through the
    handle runs under the ambient verified session.
    """
    from arbora.mcp_server.served_engine import served_engine

    return served_engine()


def _set_readiness_authority(session: Any) -> object:
    """Hand the readiness probe the process's own verified authority."""

    from arbora.observability.runtime_health import claim_readiness_authority

    return claim_readiness_authority(session)


def _release_readiness_authority(owner: object) -> None:
    """Release only the readiness authority claimed by this serving lifecycle."""

    from arbora.observability.runtime_health import release_readiness_authority

    if not release_readiness_authority(owner):
        raise RuntimeError("arbora no longer owns the readiness authority")


def _mint_process_session(transport: str) -> Any:
    """Mint the process's verified graph authority.

    Tiny packaged-local stdio uses an in-memory asymmetric authority. Every
    other topology resolves a token reference or performs OAuth2 client
    credentials, then validates the result through the same JWKS path as HTTP.
    The authority scopes background engine bootstrap for every transport and is
    additionally used for stdio tool calls, which have no request Authorization
    header.
    """
    from arbora.core.settings import runtime_config as config
    from arbora.security.anonymous import enabled
    from arbora.security.anonymous import session as anonymous_session
    from arbora.security.request_identity import (
        acquire_process_identity_token,
        local_process_authority_enabled,
        mint_actor_from_token_sync,
        mint_graph_session,
        mint_local_process_session,
    )

    if enabled():
        return anonymous_session()

    if transport == "stdio" and local_process_authority_enabled(config):
        session = mint_local_process_session()
    else:
        token = acquire_process_identity_token(config)
        actor = mint_actor_from_token_sync(token)
        from arbora.security.actor_context import CredentialLease

        expires_at = getattr(actor, "credential_expires_at", None)
        if expires_at is None:
            raise RuntimeError("Graph process identity has no bounded expiry")
        actor = replace(
            actor,
            credential_lease=CredentialLease(int(expires_at)),
        )
        session = mint_graph_session(actor)
    session_context(session)
    logger.info("Verified graph process authority minted")
    return session


class ProcessAuthorityRenewalError(RuntimeError):
    """A process-authority renewal refusal with a closed-vocabulary ``code``.

    ``code`` is safe for the unauthenticated readiness route; the message is a
    fixed sentence and never carries token, secret or endpoint material.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _process_authority_change(left: Any, right: Any) -> str | None:
    """Why a renewed token does not preserve the original authority, or ``None``.

    Identity, tenant and groups must match exactly. Roles are compared as a
    set: an identity provider may reorder them between tokens, and an additive
    grant (an operator adding a role to the service account) keeps the running
    session valid at its original, narrower role set. A revoked role fails
    closed, because the session would otherwise keep authority the identity no
    longer holds.
    """
    for name in ("actor_id", "actor_type", "tenant_id", "authenticated"):
        if getattr(left, name, None) != getattr(right, name, None):
            return "identity_changed"
    if set(getattr(left, "groups", ()) or ()) != set(
        getattr(right, "groups", ()) or ()
    ):
        return "groups_changed"
    if not set(getattr(left, "roles", ()) or ()) <= set(
        getattr(right, "roles", ()) or ()
    ):
        return "roles_revoked"
    return None


_SECRET_LIKE = re.compile(
    r"(eyJ[\w-]+\.[\w-]+\.[\w-]*|(?i:bearer)\s+\S+|[A-Za-z0-9+/_-]{32,}={0,2})"
)


def _safe_reason(exc: BaseException) -> str:
    """``Type: message`` with token-like material masked, bounded for one log line."""
    return f"{type(exc).__name__}: " + _SECRET_LIKE.sub("<masked>", str(exc))[:300]


def _cause_chain(exc: BaseException) -> str:
    """The masked ``__cause__`` chain, outermost first (at most four links)."""
    links: list[str] = []
    cause = exc.__cause__
    while cause is not None and len(links) < 4:
        links.append(_safe_reason(cause))
        cause = cause.__cause__
    return " <- ".join(links) or "-"


def _log_renewal_failure(what: str, exc: BaseException) -> None:
    """Log a renewal failure with its code, message and cause; never a secret."""
    logger.error(
        "%s renewal failed (code=%s, exception_type=%s, error=%s, cause=%s)",
        what,
        getattr(exc, "code", "renewal_failed"),
        type(exc).__name__,
        _safe_reason(exc),
        _cause_chain(exc),
    )


def _renewed_process_actor(config: Any) -> Any:
    from arbora.security.request_identity import (
        acquire_process_identity_token,
        local_process_authority_enabled,
        mint_actor_from_token_sync,
        mint_local_process_session,
    )

    if local_process_authority_enabled(config):
        return mint_local_process_session().actor
    token = acquire_process_identity_token(config)
    try:
        return mint_actor_from_token_sync(token)
    finally:
        del token


def _ensure_renewable_process_session(session: Any, lease: Any) -> None:
    """Refuse only a terminal lease; an expired lease still calls the provider.

    ARBORA-IDENTITY-R027.3: ``Degraded`` and ``Expired`` recover through the
    client-credentials flow, so this check no longer asks the session whether
    its authority is current. A ``Revoked`` lease keeps its confirmed reason.
    """
    del session
    from arbora.security.actor_context import LeaseState

    if lease.state is LeaseState.REVOKED:
        raise ProcessAuthorityRenewalError(
            lease.revocation_reason or "renewal_failed",
            "Graph process authority changed during renewal",
        )
    if lease.state is LeaseState.STOPPED:
        raise ProcessAuthorityRenewalError(
            "not_renewable", "Graph process authority is stopped"
        )


def _refresh_process_authority(session: Any, *, from_request: bool = False) -> Any:
    """Drive one renewal of the process lease without replacing captured sessions.

    External identities reacquire and validate their configured token. Tiny
    packaged-local stdio remints its in-memory asymmetric proof instead; it
    never falls through to an external-token lookup it cannot satisfy. Every
    state change goes through :meth:`CredentialLease.apply`: a ``Live`` lease
    that is not yet due returns at once; ``RenewDue`` sends ``renew_start``;
    the provider's answer is ``idp_ok``, ``idp_err`` or ``identity_changed``.
    After validation only the bounded expiry is copied into the shared lease.
    Identity, roles, tenant, route, and policy may not change.

    ``from_request`` (a dispatch-entry or keepalive refresh) never recovers an
    ``Expired`` lease: the request fails closed with ``SessionExpiredError``
    and the supervisor loop alone re-acquires, with backoff.
    """
    from arbora.core.settings import runtime_config as config
    from arbora.security.actor_context import LeaseEvent, LeaseState
    from arbora.security.run_authority import RunCredentialLease
    from arbora.security.session import SessionExpiredError

    lease = getattr(getattr(session, "actor", None), "credential_lease", None)
    if lease is None or isinstance(lease, RunCredentialLease):
        # A run lease is the caller's authority, renewed only by its run
        # (ARBORA-IDENTITY-R029); the process identity must never replace it.
        raise ProcessAuthorityRenewalError(
            "not_renewable", "Graph process authority is not renewable"
        )
    with _PROCESS_SESSION_REFRESH_LOCK:
        _ensure_renewable_process_session(session, lease)
        lease.tick()
        if lease.state is LeaseState.LIVE:
            return session
        if from_request and lease.state is LeaseState.EXPIRED:
            raise SessionExpiredError("Verified graph authority has expired")
        if lease.state is LeaseState.RENEW_DUE:
            lease.apply(LeaseEvent.RENEW_START)
        try:
            renewed_actor = _renewed_process_actor(config)
        except Exception as exc:
            lease.tick()
            lease.apply(LeaseEvent.IDP_ERR, code="acquisition_failed")
            raise ProcessAuthorityRenewalError(
                "acquisition_failed", "Graph process identity could not be renewed"
            ) from exc
        lease.tick()
        change = _process_authority_change(session.actor, renewed_actor)
        if change is not None:
            lease.apply(LeaseEvent.IDENTITY_CHANGED, code=change)
            raise ProcessAuthorityRenewalError(
                change, "Graph process authority changed during renewal"
            )
        added = sorted(
            set(getattr(renewed_actor, "roles", ()) or ())
            - set(getattr(session.actor, "roles", ()) or ())
        )
        if added:
            logger.warning(
                "Graph process identity gained roles %s; the running session "
                "keeps its original roles until the next restart",
                added,
            )
        expires_at = getattr(renewed_actor, "credential_expires_at", None)
        if expires_at is None or int(expires_at) <= int(time.time()) + 30:
            lease.apply(LeaseEvent.IDP_ERR, code="too_short_lived")
            raise ProcessAuthorityRenewalError(
                "too_short_lived", "Graph process authority renewal is too short-lived"
            )
        lease.apply(LeaseEvent.IDP_OK, expires_at=int(expires_at))
        return session


async def _ensure_process_authority_current() -> Any:
    """Ensure request/process authority without blocking the MCP event loop.

    D-SNV-5: an ambient session whose actor carries a renewable
    ``credential_lease`` (a server-minted process/client-credentials identity
    — never a caller-presented bearer JWT, which has no ``credential_lease``
    and stays exactly as fail-closed as before) is proactively renewed here
    the same way the stdio ``_PROCESS_SESSION`` fallback already was. This is
    the dispatch-entry check only; :func:`_keep_process_authority_current`
    covers the rest of a long-running dispatch.
    """
    from arbora.security.run_authority import RunCredentialLease
    from arbora.security.session import (
        SessionExpiredError,
        current_session,
    )

    ambient = current_session()
    if ambient is not None:
        ambient_lease = getattr(
            getattr(ambient, "actor", None), "credential_lease", None
        )
        if ambient_lease is None or isinstance(ambient_lease, RunCredentialLease):
            # Not server-renewable: unchanged fail-closed behavior, no
            # minimum-TTL headroom requirement — this must never mask a
            # caller's real credential expiry.
            ambient.ensure_authority_current()
            return ambient
        try:
            ambient.ensure_authority_current(minimum_ttl_seconds=30)
        except SessionExpiredError:
            ambient = await asyncio.to_thread(
                _refresh_process_authority, ambient, from_request=True
            )
        if ambient is None:
            # `_refresh_process_authority` is typed `Any` (it renews in place and
            # returns the same session), so this should be unreachable in
            # practice — but if it ever did return nothing, failing closed with
            # the same PermissionError the no-ambient-session branch below uses
            # is correct, not an opaque AttributeError on the next line.
            raise PermissionError("Verified GraphSession required")
        ambient.ensure_authority_current(minimum_ttl_seconds=30)
        return ambient
    session = _process_session()
    if session is None:
        raise PermissionError("Verified GraphSession required")
    try:
        session.ensure_authority_current(minimum_ttl_seconds=30)
    except SessionExpiredError:
        session = await asyncio.to_thread(
            _refresh_process_authority, session, from_request=True
        )
    session.ensure_authority_current(minimum_ttl_seconds=30)
    return session


async def _keep_process_authority_current(session: Any) -> None:
    """Background keepalive for one in-flight dispatch under a renewable session.

    A tool dispatch may run for the whole ``_TOOL_CALL_TIMEOUT_S`` window
    (``_execute_tool``), but authority was previously checked only once, at
    entry (D-SNV-5: a real 192s ServiceNow delegation died mid-flight with
    ``SessionExpiredError`` because nothing renewed it after that). This loop
    renews the SAME mutable ``CredentialLease`` the dispatch's ambient
    ``GraphSession.actor`` already holds, so every downstream authority check
    (``GraphSession.ensure_authority_current`` at every engine boundary, e.g.
    ``graph_compute.py``'s ``_invoke_at``) sees it transparently — no session
    object is replaced and no verification is weakened; a caller-presented
    bearer JWT (no lease) never reaches this function at all.

    Runs only for the lifetime the caller's :func:`authority_keepalive_scope`
    is open (started and cancelled there) — never a free-running background
    task.
    """
    from arbora.security.session import SessionExpiredError

    lease = session.actor.credential_lease
    try:
        while True:
            seconds_left = lease.expires_at - int(time.time())
            await asyncio.sleep(max(1.0, min(30.0, seconds_left - 30.0)))
            try:
                await asyncio.to_thread(
                    _refresh_process_authority, session, from_request=True
                )
            except SessionExpiredError:
                # Fail-closed: the next real authority check (the deep engine
                # call already in flight, or the next one) raises for real.
                # D-SWG-3: log it here too so the keepalive giving up is visible
                # at the moment it happens, not only inferred later from a
                # downstream failure.
                logger.warning(
                    "Delegation authority keepalive stopping: session lease "
                    "already expired; the next authority check will fail closed"
                )
                return
            except Exception as exc:  # noqa: BLE001 — keepalive is best-effort renewal; a hard failure surfaces at the next real authority check either way
                _log_renewal_failure("Delegation authority keepalive", exc)
    except asyncio.CancelledError:
        # D-SWG-3: expected, normal shutdown — the caller's
        # authority_keepalive_scope cancels this loop when the wrapped call
        # finishes. Logged at debug (routine, high-frequency) rather than
        # dropped silently.
        logger.debug("Delegation authority keepalive cancelled (scope exited)")


@contextlib.asynccontextmanager
async def authority_keepalive_scope(session: Any | None = None) -> AsyncIterator[None]:
    """Renew a renewable session's authority for the duration of the wrapped call.

    CONCEPT:MERISTEM-ORCH.execution.delegation-hot-path-authority — keep a long delegation authorized for its whole run, on every entrypoint, not just MCP dispatch.

    Every delegation entrypoint — the MCP ``_execute_tool`` dispatch, the
    ``agent-webui``/REST gateway, the messaging router (Telegram/Mattermost), the
    autonomous ``agent_dispatch_worker``, ``org_runtime``, a governed dynamic
    workflow, and the parallel engine — converges on the single function
    :meth:`agent_utilities.orchestration.manager.Orchestrator.execute_agent`.
    A background keepalive wired ONLY into ``_execute_tool`` (the original
    D-SNV-5 fix) therefore covered MCP-dispatched delegations only; anything that
    reached ``execute_agent`` some other way still expired mid-flight with
    ``SessionExpiredError``. This context manager is the one reusable primitive
    both ``_execute_tool`` and ``Orchestrator.execute_agent`` open, so every
    surface inherits the renewal by construction instead of each caller
    reimplementing it.

    Security posture is unchanged from the original ``_execute_tool``-only fix:

    * Only a server-minted, renewable ``credential_lease`` is ever proactively
      renewed. A caller-presented bearer JWT carries no ``credential_lease`` and
      is never touched here — it stays exactly as fail-closed as before.
    * The SAME mutable :class:`~arbora.security.actor_context.CredentialLease`
      object the session already holds is renewed in place, so every downstream
      authority check (``GraphSession.ensure_authority_current``, e.g.
      ``graph_compute.py``'s ``_invoke_at``) sees it transparently — no session
      object is replaced and the expiry check itself is never weakened or
      lengthened.
    * ``session`` defaults to the ambient :func:`GraphSession
      <arbora.security.session.current_session>` (falling
      back to the stdio ``_PROCESS_SESSION``, same as ``verified_tool_session_scope``)
      resolved WITHOUT raising when neither is set — this helper only ever adds
      renewal; it must never introduce a new precondition. A caller with no
      ambient/process session at all fails exactly as it already would, at the
      first real engine boundary (``SessionRequiredError``), not here.

    Idempotent/reentrant via :data:`_AUTHORITY_KEEPALIVE_ACTIVE`: a scope nested
    inside another already-open scope on the same task tree (a nested MCP
    dispatch that itself calls ``execute_agent``, or vice versa) is a no-op —
    the outer scope already renews the same lease, and the extra loop would only
    add redundant token round-trips, not extra safety.
    """
    if _AUTHORITY_KEEPALIVE_ACTIVE.get():
        yield
        return

    if session is None:
        from arbora.security.session import current_session

        session = current_session() or _process_session()
    if session is None:
        yield
        return

    lease = getattr(getattr(session, "actor", None), "credential_lease", None)
    if lease is None:
        async with _caller_run_authority_scope(session):
            yield
        return

    guard_token = _AUTHORITY_KEEPALIVE_ACTIVE.set(True)
    keepalive = asyncio.ensure_future(_keep_process_authority_current(session))
    try:
        yield
    finally:
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive
        _AUTHORITY_KEEPALIVE_ACTIVE.reset(guard_token)


@contextlib.asynccontextmanager
async def _caller_run_authority_scope(session: Any) -> AsyncIterator[None]:
    """Renew a caller's verified authority for the life of one run.

    ARBORA-IDENTITY-R029: a caller bearer lives 300 s, but a run it starts may
    run longer. The run gets a :class:`RunCredentialLease` seeded from the
    verified bearer expiry and becomes the ambient session for the run's body,
    so every fan-out child created inside it inherits the same lease (a nested
    scope is a no-op under :data:`_AUTHORITY_KEEPALIVE_ACTIVE`). The renewer
    extends the lease while the run is active and the caller is not revoked,
    up to the configured maximum run lifetime. Leaving the scope (run end or
    cancellation) stops renewal and revokes the lease at once. No token is
    read, held, logged or forwarded.
    """
    from arbora.security.actor_context import CredentialExpiredError
    from arbora.security.run_authority import (
        CALLER_REVOCATIONS,
        _log_audit,
        audit_record,
        caller_run_renewable,
        keep_run_authority_current,
        open_run_lease,
        run_renewal_policy,
    )
    from arbora.security.session import use_session

    actor = getattr(session, "actor", None)
    if actor is None or not caller_run_renewable(actor):
        yield
        return
    policy = run_renewal_policy()
    try:
        lease = open_run_lease(actor, policy=policy)
    except CredentialExpiredError:
        # Already expired: unchanged fail-closed behavior at the next check.
        yield
        return
    run_actor = replace(actor, credential_lease=lease)
    run_session = session.with_actor(run_actor)
    CALLER_REVOCATIONS.register(run_actor, lease)
    _log_audit(audit_record("started", run_actor, lease))
    guard_token = _AUTHORITY_KEEPALIVE_ACTIVE.set(True)
    renewer = asyncio.ensure_future(
        keep_run_authority_current(run_actor, lease, policy=policy)
    )
    ended = "run_ended"
    try:
        with use_session(run_session):
            yield
    except asyncio.CancelledError:
        ended = "run_cancelled"
        raise
    finally:
        renewer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renewer
        lease.revoke(ended)
        CALLER_REVOCATIONS.unregister(run_actor, lease)
        _log_audit(audit_record(ended, run_actor, lease))
        _AUTHORITY_KEEPALIVE_ACTIVE.reset(guard_token)


#: Backoff of a failing process-lease renewal (ARBORA-IDENTITY-R027.2): the
#: delay before attempt ``n + 1`` is ``min(cap, 2 ** (n - 1))`` seconds times a
#: jitter factor drawn from ``[low, high]``.
PROCESS_RENEWAL_BACKOFF_CAP_SECONDS = 30.0
PROCESS_RENEWAL_JITTER_LOW = 0.5
PROCESS_RENEWAL_JITTER_HIGH = 1.0
#: The longest a ``Live`` lease sleeps before it looks at its window again.
PROCESS_RENEWAL_MAX_IDLE_SECONDS = 60.0


def next_delay(attempt: int, rng: Any) -> float:
    """The jittered exponential delay after ``attempt`` failed renewals."""
    base = min(PROCESS_RENEWAL_BACKOFF_CAP_SECONDS, 2.0 ** (max(1, int(attempt)) - 1))
    return base * rng.uniform(PROCESS_RENEWAL_JITTER_LOW, PROCESS_RENEWAL_JITTER_HIGH)


def _process_authority_refresh_loop(session: Any, rng: Any = None) -> None:
    """Keep a renewable process lease current for all captured worker sessions.

    The loop runs for the life of the process. A ``Live`` lease sleeps until
    half of its lifetime has passed; then the lease renews. A failed renewal
    leaves it ``Degraded`` (or ``Expired`` once the expiry passes) and is
    retried with jittered exponential backoff, never exiting, until the
    provider answers and the lease is ``Live`` again (ARBORA-IDENTITY-R027.2,
    R027.3). Only a terminal lease (``Revoked``, ``Stopped``) ends the loop.
    Readiness reads the lease state directly (R027.5).
    """
    from arbora.security.actor_context import TERMINAL_LEASE_STATES, LeaseState

    lease = getattr(getattr(session, "actor", None), "credential_lease", None)
    if lease is None:
        return
    rng = rng or random.Random()  # nosec B311 - backoff jitter, not crypto
    while not _PROCESS_AUTHORITY_STOP.is_set():
        lease.tick()
        if lease.state in TERMINAL_LEASE_STATES:
            return
        if lease.state is LeaseState.LIVE:
            idle = lease.renew_due_at - time.time()
            _PROCESS_AUTHORITY_STOP.wait(
                min(PROCESS_RENEWAL_MAX_IDLE_SECONDS, max(1.0, idle))
            )
            continue
        try:
            _refresh_process_authority(session)
        except Exception as exc:  # noqa: BLE001 - retry; expiry remains fail-closed
            _log_renewal_failure("Graph process authority", exc)
            if lease.state in TERMINAL_LEASE_STATES:
                return
            _PROCESS_AUTHORITY_STOP.wait(next_delay(lease.attempts, rng))


def _start_process_authority_supervisor(session: Any) -> None:
    """Start the sole external-authority renewal supervisor when required."""
    global _PROCESS_AUTHORITY_THREAD
    from arbora.observability.runtime_health import register_process_lease

    _stop_process_authority_supervisor()
    _PROCESS_AUTHORITY_STOP.clear()
    lease = getattr(getattr(session, "actor", None), "credential_lease", None)
    if lease is None:
        return
    register_process_lease(lease)
    thread = threading.Thread(
        target=_process_authority_refresh_loop,
        args=(session,),
        daemon=True,
        name="GraphProcessAuthority",
    )
    _PROCESS_AUTHORITY_THREAD = thread
    thread.start()


def _stop_process_authority_supervisor(*, close_lease: bool = False) -> None:
    """Stop and forget the process-authority supervisor.

    ``close_lease`` (process shutdown) also sends ``stop`` to the registered
    lease, so a captured session can no longer be used. A restart of the
    supervisor for the same session leaves the lease as it is.
    """
    global _PROCESS_AUTHORITY_THREAD
    from arbora.observability.runtime_health import (
        process_lease,
        register_process_lease,
    )

    _PROCESS_AUTHORITY_STOP.set()
    thread = _PROCESS_AUTHORITY_THREAD
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=2.0)
    _PROCESS_AUTHORITY_THREAD = None
    lease = process_lease()
    if close_lease and lease is not None:
        lease.stop()
    register_process_lease(None)


_BUNDLED_SKILL_READINESS: dict[str, Any] = {}
# A 9–10 GiB four-shard graph can legitimately need just over five minutes to
# rebuild its lazy-open indexes on slower storage. Keep this within the
# deployment's ten-minute startup probe while avoiding a pointless restart at
# the former five-minute boundary, which merely repeated the cold-open work.
_ENGINE_MATERIALIZATION_TIMEOUT_SECONDS = 540.0
# Materialization progresses in durable page batches measured in seconds on a
# large graph.  Four manifest reads per second added ~1,200 Python→native calls
# to a five-minute cold open without improving correctness.  One authoritative
# poll per second keeps readiness latency bounded while leaving the engine's
# foreground read lane available for the materializer and health probes.
_ENGINE_MATERIALIZATION_POLL_SECONDS = 1.0


def _set_bundled_skill_readiness(report: dict[str, Any]) -> None:
    """Publish packaged-skill readiness so /health can report it.

    Readiness no longer gates boot, so it MUST be observable at runtime —
    otherwise "serving degraded" is indistinguishable from "fully ready" to
    anything outside the process, which is the silent-failure pattern this
    codebase keeps getting bitten by.
    """
    _BUNDLED_SKILL_READINESS.clear()
    _BUNDLED_SKILL_READINESS.update(report)


def bundled_skill_readiness() -> dict[str, Any]:
    """The last packaged-skill readiness report (empty before bootstrap runs)."""
    return dict(_BUNDLED_SKILL_READINESS)


def _resolve_materialization_handles(engine: Any) -> tuple[Any, Any, str]:
    """Resolve the engine's native ``(query_cypher, list_graphs, graph_name)``.

    ``query_cypher``/``list_graphs`` come back ``None`` when the engine
    doesn't participate in native lifecycle (lightweight test engines and
    non-native backends have neither ``client`` nor ``graph_name``) or has
    no graph name at all — the caller treats either as "not_applicable".

    Arbora owns the high-level IntelligenceGraphEngine; the lifecycle
    manifest belongs to its native GraphComputeEngine authority. Test tools
    and lower-level callers may pass that authority directly.
    """
    native_engine = getattr(engine, "graph_compute", None) or engine
    client = getattr(native_engine, "client", None)
    graph_name = str(getattr(native_engine, "graph_name", "") or "")
    query_cypher = getattr(native_engine, "query_cypher", None)
    tenants = getattr(client, "tenants", None)
    list_graphs = getattr(tenants, "list", None)
    if not graph_name or not callable(query_cypher) or not callable(list_graphs):
        return None, None, graph_name
    return query_cypher, list_graphs, graph_name


def _engine_read_probe_status(query_cypher: Any) -> str:
    """Run the one bounded read that both triggers and probes materialization.

    Returns ``"complete"`` (the read succeeded), ``"partial"`` (the read hit
    ``PARTIAL_MATERIALIZATION`` — the caller should keep polling the
    manifest), or ``"absent"`` (the graph does not exist). Any other
    exception propagates unchanged.
    """
    try:
        query_cypher("MATCH (n) RETURN n.id AS id LIMIT 1")
    except Exception as exc:
        # Control-flow only: this text never reaches a log or a caller, so it
        # is read via `exc.args` (never `str(exc)`/`repr(exc)`) to stay clear
        # of the served-boundary exception-surface policy on principle.
        detail = str(exc.args[0]) if exc.args else ""
        if "PARTIAL_MATERIALIZATION" in detail:
            return "partial"
        if "not found" in detail.lower():
            return "absent"
        raise
    return "complete"


def _resolve_manifest_entry(
    list_graphs: Any, graph_name: str, manifest_visible: bool | None
) -> tuple[dict[str, Any] | None, bool | None]:
    """Resolve this graph's manifest entry from ``list_graphs()``.

    Respects the "already known hidden" cache — once a poll has found the
    graph absent from the catalog (RLS-filtered), later polls skip
    re-querying the whole catalog and go straight to the read-probe
    fallback. Returns ``(entry, manifest_visible)``.
    """
    if manifest_visible is False:
        return None, manifest_visible
    entries = list_graphs() or []
    entry = next(
        (
            value
            for value in entries
            if (value.get("name") if isinstance(value, dict) else None) == graph_name
        ),
        None,
    )
    return entry, isinstance(entry, dict)


def _handle_materialized_manifest_entry(
    entry: dict[str, Any],
    graph_name: str,
    last_progress: tuple[str, int | None, int | None] | None,
) -> tuple[dict[str, Any] | None, tuple[str, int | None, int | None] | None]:
    """Interpret one polled, catalog-visible manifest entry.

    Returns ``(result, updated_last_progress)`` where ``result`` is the
    barrier's terminal return value once materialization is complete, or
    ``None`` to keep polling. Raises on a ``failed`` materialization phase.
    """
    phase = str(entry.get("materialization") or "unknown")
    valid = entry.get("valid") is True
    cursor = entry.get("completeness_cursor")
    node_offset = cursor.get("node_offset") if isinstance(cursor, dict) else None
    edge_offset = cursor.get("edge_offset") if isinstance(cursor, dict) else None
    progress = (phase, node_offset, edge_offset)
    if progress != last_progress:
        logger.info(
            "Epistemic graph materialization progress "
            "(graph=%s phase=%s node_offset=%s edge_offset=%s)",
            graph_name,
            phase,
            node_offset,
            edge_offset,
        )
        last_progress = progress
    if phase == "complete" and valid:
        logger.info(
            "Epistemic graph materialization ready "
            "(graph=%s node_offset=%s edge_offset=%s)",
            graph_name,
            node_offset,
            edge_offset,
        )
        return dict(entry), last_progress
    if phase == "failed":
        raise RuntimeError(
            "epistemic graph materialization failed "
            f"(graph={graph_name!r}, cursor={cursor!r})"
        )
    return None, last_progress


def _handle_hidden_manifest_entry(
    query_cypher: Any, graph_name: str
) -> dict[str, Any] | None:
    """Handle a poll where the graph's manifest entry is not catalog-visible.

    RLS may intentionally hide a protected graph (for example
    ``__secrets__``) from the catalog while still authorizing this
    process-scoped graph view. In that case the same bounded read that
    triggered materialization is the authoritative completion probe. Do not
    weaken catalog RLS merely to make boot observable. Returns a terminal
    result dict, or ``None`` to keep polling.
    """
    status = _engine_read_probe_status(query_cypher)
    if status == "absent":
        return {"graph": graph_name, "materialization": "absent"}
    if status == "complete":
        logger.info(
            "Epistemic graph materialization ready via authorized "
            "read probe (graph=%s; catalog manifest hidden)",
            graph_name,
        )
        return {
            "graph": graph_name,
            "materialization": "complete",
            "valid": True,
            "manifest_visible": False,
        }
    return None


def _initial_materialization_probe(
    engine: Any,
) -> tuple[dict[str, Any] | None, Any, Any, str]:
    """Resolve native handles, then run the initial bounded-read probe.

    Returns ``(early_result, query_cypher, list_graphs, graph_name)``.
    ``early_result`` is non-``None`` when the caller should return it
    immediately (``not_applicable`` / ``absent`` / ``complete``) rather than
    entering the manifest poll loop.
    """
    query_cypher, list_graphs, graph_name = _resolve_materialization_handles(engine)
    if query_cypher is None or list_graphs is None:
        early = {"graph": graph_name or None, "materialization": "not_applicable"}
        return early, query_cypher, list_graphs, graph_name

    status = _engine_read_probe_status(query_cypher)
    if status == "absent":
        return (
            {"graph": graph_name, "materialization": "absent"},
            query_cypher,
            list_graphs,
            graph_name,
        )
    if status == "complete":
        return (
            {"graph": graph_name, "materialization": "complete", "valid": True},
            query_cypher,
            list_graphs,
            graph_name,
        )
    return None, query_cypher, list_graphs, graph_name


def _poll_materialization_step(
    list_graphs: Any,
    query_cypher: Any,
    graph_name: str,
    last_progress: tuple[str, int | None, int | None] | None,
    manifest_visible: bool | None,
) -> tuple[
    dict[str, Any] | None,
    tuple[str, int | None, int | None] | None,
    bool | None,
]:
    """Run one manifest-poll iteration of :func:`_wait_for_engine_materialization`.

    Returns ``(result, last_progress, manifest_visible)``; ``result`` is the
    barrier's terminal return value once resolved, or ``None`` to keep
    polling.
    """
    entry, manifest_visible = _resolve_manifest_entry(
        list_graphs, graph_name, manifest_visible
    )
    if isinstance(entry, dict):
        result, last_progress = _handle_materialized_manifest_entry(
            entry, graph_name, last_progress
        )
        return result, last_progress, manifest_visible
    result = _handle_hidden_manifest_entry(query_cypher, graph_name)
    return result, last_progress, manifest_visible


def _wait_for_engine_materialization(
    engine: Any,
    *,
    timeout_seconds: float = _ENGINE_MATERIALIZATION_TIMEOUT_SECONDS,
    poll_seconds: float = _ENGINE_MATERIALIZATION_POLL_SECONDS,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Wait for a lazy-open graph to become complete before workers begin.

    The epistemic engine deliberately serves its catalog before a cold graph has
    finished paging into memory.  Reads and writes against that graph correctly
    fail with ``PARTIAL_MATERIALIZATION`` until its durable manifest is both
    ``complete`` and ``valid``. Arbora task workers are not ordinary retrying
    callers: starting them during that interval can discard claimed work. This
    barrier starts lazy-open with one bounded
    read, then observes the authoritative ``ListGraphs`` manifest until it is
    safe to claim queued work.

    Lightweight test engines and non-native backends have neither ``client`` nor
    ``graph_name`` and therefore do not participate in this native lifecycle.
    A graph absent from a new empty engine is likewise left for normal creation.
    """
    early_result, query_cypher, list_graphs, graph_name = (
        _initial_materialization_probe(engine)
    )
    if early_result is not None:
        return early_result

    logger.info(
        "Arbora waiting for epistemic graph materialization before worker startup "
        "(graph=%s timeout_seconds=%g)",
        graph_name,
        timeout_seconds,
    )
    shutdown = stop_event or _PROCESS_AUTHORITY_STOP
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_progress: tuple[str, int | None, int | None] | None = None
    manifest_visible: bool | None = None
    while True:
        result, last_progress, manifest_visible = _poll_materialization_step(
            list_graphs, query_cypher, graph_name, last_progress, manifest_visible
        )
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "epistemic graph did not become completely materialized before "
                f"Arbora worker startup (graph={graph_name!r}, "
                f"timeout_seconds={timeout_seconds:g}, last={last_progress!r})"
            )
        if shutdown.wait(max(0.0, poll_seconds)):
            raise InterruptedError(
                "Arbora shutdown cancelled the graph materialization barrier"
            )


def _engine_bootstrap_is_client(engine: Any, fallback_role: str) -> bool:
    """Return whether either the requested or elected engine role is client."""
    requested_role = (
        (getattr(engine, "_daemon_role", None) or fallback_role).strip().lower()
    )
    effective_role = (
        (getattr(engine, "_effective_role", None) or requested_role).strip().lower()
    )
    return "client" in {requested_role, effective_role}


def _engine_ready_timeout_seconds() -> float:
    """How long serving start waits for the engine (``ARBORA_ENGINE_READY_TIMEOUT``)."""
    from arbora.core.settings import runtime_config

    return float(getattr(runtime_config, "arbora_engine_ready_timeout", 1500.0))


def _start_engine_bootstrap(session: Any) -> None:
    """Prove the engine answers under the process authority before serving.

    EG owns its workers, materialization and durable state
    (MEDULLA-INGEST-WORKER-R604); arbora starts none of them. Startup only
    connects the shared pool and reads EG's health document under the minted
    process session, retrying until the endpoint answers or the bounded
    deadline passes, so an unreachable engine stops startup with its cause.
    """
    from arbora.security.session import use_session

    engine = _get_engine()
    graph = str(session.graph)
    deadline = time.monotonic() + _engine_ready_timeout_seconds()
    while True:
        try:
            with use_session(session):
                health = asyncio.run(engine.health(graph))
        except (ConnectionError, OSError, TimeoutError) as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "epistemic-graph did not answer before serving start"
                ) from exc
            logger.info("waiting for the engine to answer: %s", type(exc).__name__)
            if _PROCESS_AUTHORITY_STOP.wait(1.0):
                raise InterruptedError("shutdown cancelled engine bootstrap") from exc
            continue
        logger.info(
            "Epistemic graph engine answered (status=%s)",
            health.get("status") if isinstance(health, dict) else "unknown",
        )
        return


def _process_session() -> Any:
    """The stdio process session; ``runtime`` holds the one live binding."""
    from arbora.mcp_server import runtime

    return runtime._PROCESS_SESSION


def set_process_session(session: Any) -> None:
    """Bind the process session on the host runtime (its single owner)."""
    from arbora.mcp_server import runtime

    runtime._PROCESS_SESSION = session


BOOTSTRAP_EXPORTS = tuple(
    name
    for name, value in globals().items()
    if getattr(value, "__module__", None) == __name__ or name.startswith("DEFAULT_")
)
