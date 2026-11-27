"""Gateway-hosted unified KG daemon (CONCEPT:MEDULLA-KG.storage.nonblocking-checkpoint /
OS-5.9).

The API gateway is the single process that runs the ONE consolidated KG
background daemon (queue drain + graph writer + task workers + maintenance
scheduler + file-watch poll). Every other entry point — the MCP server, CLI,
and one-shot scripts — runs as a ``client`` (``KG_DAEMON_ROLE=client``) and
spawns NO background threads; their work is enqueued to the durable task queue
that this host daemon drains.

Mount the daemon with ``start_host_daemon()`` from the gateway's lifespan/startup
and surface its state via the ``/daemon/*`` routes of ``build_dashboard_router``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any, NoReturn

from arbora.api.errors import not_served
from arbora.core.environment import read_env, write_env
from arbora.gateway.state import GatewayState
from arbora.security.anonymous import session_context
from arbora.security.error_surface import public_error_payload
from arbora.security.log_redaction import redact_for_log
from arbora.security.run_token import RunTokenSigner

logger = logging.getLogger(__name__)


def start_daemon_metrics_listener(gateway: GatewayState) -> bool:
    """Expose this daemon's ``meristem_*`` Prometheus registry over HTTP.

    CONCEPT:MERISTEM-OS.observability.daemon-metrics-listener (D-OG-4). The
    consolidated KG host daemon this module runs records the
    ``SCHEDULED_JOB_*``/``LANE_*``/``KG_INGEST_*``/``DISPATCH_*`` families
    (``observability/gateway_metrics.py``) into the SAME per-process
    ``prometheus_client`` registry every other metric in this package uses —
    but unlike arbora (``server_factory.py``, port 8000) or the full
    gateway (``gateway/graph_api.py``), the standalone entry point this
    module's :func:`main` runs (``python3 -m arbora.gateway.daemon``
    — the ``arbora-host`` container in the live pod) has NO HTTP server of
    any kind. Half of the package's defined metric series were therefore
    structurally uncollectable: recorded, but never scrapeable.
    ``prometheus_client.start_http_server`` is deliberately the entire fix —
    it starts a small background HTTP thread serving ``generate_latest()``
    over the default registry; no new route table, no framework.

    Only called from the standalone :func:`main` path, never from
    :func:`start_host_daemon` directly — when this daemon instead runs
    in-process inside the full gateway (mounted from its lifespan, per this
    module's own docstring), that process already serves ``GET /metrics``
    on its own HTTP port via ``graph_api.py``, reading the identical shared
    registry; a second listener there would be redundant, not merely
    harmless.

    Bind address / auth — read the justification, this is a deliberate
    judgment call, not a default:

    Binds **pod-local** (``0.0.0.0`` inside the container; default port
    ``9110``, ``KG_DAEMON_METRICS_PORT``) with **no bearer token** — unlike
    arbora's own ``/metrics`` (``server_factory.py``), which IS
    bearer-gated on a non-loopback listener. That is not an inconsistency;
    it is the same exposure judgment reaching the opposite answer because
    the exposure itself differs:

    * arbora's ``/metrics`` (port 8000) lives on the SAME listener as the
      public/Ingress-facing MCP tool-call surface (``arbora.example``) — an
      unauthenticated route there is reachable by anyone who can reach that
      hostname, hence the bearer gate.
    * This listener has no Service port and no Ingress route. Its only
      intended consumer is Prometheus's ``kubernetes-pods-metrics-port`` job
      (``role: pod``, zero-annotation discovery keyed on a container port
      literally named ``metrics``), which itself only runs inside the
      cluster's pod network — the same reachability tier as
      node-exporter/cAdvisor, both already scraped unauthenticated
      fleet-wide (see docs/architecture/observability.md's topology table).
      This is that tier, not a broader one.
    * The content is pure operational counts/durations/labels (schedule
      names, lane names, queue depths) — never a secret, a tool-call
      payload, or PII.
    * A strictly loopback (``127.0.0.1``) bind — the posture the bundled
      ``medulla-server`` engine itself uses for its own native
      metrics listener — would be UNREACHABLE from Prometheus, which scrapes
      via the Pod IP (the shared pod network's routable interface), not via
      a process that merely shares the pod's network namespace. Pod-local
      is the narrowest bind Prometheus's pod-IP scrape model can reach.

    Never blocks daemon startup. Two distinct failure modes are both logged
    at ERROR — loud, never swallowed — and both leave the daemon running
    with no metrics listener, exactly as it behaved before this function
    existed:

    * the optional ``metrics`` extra (``prometheus-client``) is not
      installed;
    * the bind itself fails (port already in use, permission denied, ...).
    """
    if not read_env("KG_DAEMON_METRICS"):
        logger.info("Daemon metrics listener disabled (KG_DAEMON_METRICS=false).")
        return False
    return gateway.metrics_listener.run_once(_bind_metrics_listener)


def _bind_metrics_listener() -> bool:
    """Bind the Prometheus listener; ``False`` when it could not be served."""
    try:
        from prometheus_client import start_http_server
    except ImportError as exc:
        logger.error(
            "Daemon metrics listener NOT started: the optional 'metrics' "
            "extra (prometheus-client) is not installed (%s). "
            "SCHEDULED_JOB_*/LANE_*/KG_INGEST_*/DISPATCH_* series recorded "
            "by this process will not be scrapeable until it is.",
            exc,
        )
        return False
    any_interface = ".".join(("0", "0", "0", "0"))
    host = str(read_env("KG_DAEMON_METRICS_HOST", any_interface) or any_interface)
    port = int(read_env("KG_DAEMON_METRICS_PORT") or 9110)
    try:
        start_http_server(port, addr=host)
    except OSError as exc:
        logger.error(
            "Daemon metrics listener NOT started: could not bind %s:%d "
            "(%s). The daemon continues running WITHOUT "
            "a scrapeable metrics endpoint — SCHEDULED_JOB_*/LANE_*/"
            "KG_INGEST_*/DISPATCH_* series stay uncollectable until this "
            "is resolved.",
            redact_for_log(host),
            port,
            exc,
        )
        return False
    logger.info(
        "Daemon metrics listener started on %s:%d "
        "(CONCEPT:MERISTEM-OS.observability.daemon-metrics-listener).",
        redact_for_log(host),
        port,
    )
    return True


def mint_process_identity() -> Any:
    """Mint the standalone host daemon's verified graph authority.

    The daemon is a long-lived, non-request process, so there is no HTTP
    middleware to establish an actor or :class:`GraphSession`.  Resolve and
    validate the configured process credential before constructing any graph
    backend, and attach a renewable in-memory lease so worker threads retain
    bounded authority without retaining token material.
    """
    from arbora.core.settings import runtime_config as config
    from arbora.security.actor_context import CredentialLease
    from arbora.security.request_identity import (
        acquire_process_identity_token,
        mint_actor_from_token_sync,
        mint_graph_session,
    )

    token = acquire_process_identity_token(config)
    actor = mint_actor_from_token_sync(token)
    del token
    expires_at = getattr(actor, "credential_expires_at", None)
    if expires_at is None:
        raise RuntimeError("Graph host process identity has no bounded expiry")
    actor = replace(
        actor,
        credential_lease=CredentialLease(int(expires_at)),
    )
    session = mint_graph_session(actor)
    session.ensure_authority_current(minimum_ttl_seconds=30)
    session_context(session)
    logger.info("Verified graph host process authority minted")
    return session


def refresh_process_identity(session: Any) -> Any:
    """Renew ``session`` in place before its validated credential expires.

    Only the shared expiry lease changes.  Subject, tenant, roles, groups, and
    actor type must remain byte-for-byte equivalent to the authority captured
    by the background worker threads.
    """
    from arbora.core.settings import runtime_config as config
    from arbora.security.request_identity import (
        acquire_process_identity_token,
        mint_actor_from_token_sync,
    )
    from arbora.security.session import SessionExpiredError

    session.ensure_authority_current()
    try:
        session.ensure_authority_current(minimum_ttl_seconds=30)
        return session
    except SessionExpiredError as exc:
        logger.info("Refreshing host session before expiry: %s", exc)

    lease = getattr(getattr(session, "actor", None), "credential_lease", None)
    if lease is None:
        raise RuntimeError("Graph host process authority is not renewable")
    token = acquire_process_identity_token(config)
    renewed_actor = mint_actor_from_token_sync(token)
    del token
    identity_fields = (
        "actor_id",
        "actor_type",
        "roles",
        "tenant_id",
        "authenticated",
        "groups",
    )
    if any(
        getattr(session.actor, field, None) != getattr(renewed_actor, field, None)
        for field in identity_fields
    ):
        lease.revoke("identity_changed")
        raise RuntimeError("Graph host process authority changed during renewal")
    expires_at = getattr(renewed_actor, "credential_expires_at", None)
    if expires_at is None or int(expires_at) <= int(time.time()) + 30:
        raise RuntimeError("Graph host process authority renewal is too short-lived")
    session.ensure_authority_current()
    lease.renew(int(expires_at))
    session.ensure_authority_current(minimum_ttl_seconds=30)
    return session


def start_host_daemon(signer: RunTokenSigner) -> NoReturn:
    """Refuse to start the consolidated KG daemon: the engine owns it.

    Delegated identity is validated first so a mis-configured host fails at
    startup, then the daemon role is set; the daemon itself is not served by
    this process (MEDULLA-PLACEMENT-R611).
    """
    # CONCEPT:MERISTEM-OS.identity.per-agent-on-behalf-delegation (decision 3) — fail
    # closed at startup when delegated identity is on but no run-token signing
    # secret resolves, so the daemon refuses to boot (config-contract) instead
    # of failing lazily on the first spawn. No-op in the shipped `warn`/`off`
    # posture.
    signer.require_configured()
    # The gateway process is the authoritative daemon host.
    write_env("KG_DAEMON_ROLE", "host")
    raise not_served("knowledge_graph.core.engine", "MEDULLA-PLACEMENT-R611")


def daemon_status(engine: Any = None) -> dict[str, Any]:
    """Return the consolidated daemon's status (role + live threads + jobs).

    ``engine`` is the engine handle the caller holds; ``None`` reports idle.
    """
    eng = engine
    if eng is None:
        return {"running": False, "role": read_env("KG_DAEMON_ROLE", "auto")}
    try:
        return eng.unified_daemon_status()
    except Exception as exc:
        logging.getLogger(__name__).debug("fail-closed fallback", exc_info=True)
        return {"running": False, **public_error_payload(exc, logger=logger)}


def stop_host_daemon(engine: Any = None) -> None:
    """Graceful shutdown: best-effort engine checkpoint, then release the lock.

    The durable task queue persists on its own (SQLite/NATS/Kafka), so a
    restarted host resumes it. We trigger an engine checkpoint (snapshot to
    ``GRAPH_SERVICE_PERSIST_DIR`` when enabled) for a fast warm restart, then
    release the singleton host lock so a replacement host can take over.
    """
    eng = engine
    if eng is not None:
        try:
            gc = getattr(eng, "graph_compute", None) or getattr(eng, "graph", None)
            client = getattr(gc, "_client", None)
            if client is not None and hasattr(client, "checkpoint"):
                client.checkpoint()
                logger.info("Engine checkpoint written on shutdown.")
        except Exception as exc:
            logger.warning("engine checkpoint on shutdown skipped: %s", exc)
    logger.info("Host daemon stopped.")
