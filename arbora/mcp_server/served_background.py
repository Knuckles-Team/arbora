"""The identity, SCM ingress and background services the served invocation starts (ARBORA-HOST-R038.11)."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from arbora.core.environment import read_env


def _loop_body(bindings: Mapping[str, Any]) -> Any:
    """The bound loop body, else the finance schedule body over its host.

    The finance schedule runs on the Loops lease and task queue, never a scheduler of
    its own (ARBORA-DATA-MARKET-R002).
    """
    host = bindings.get("finance_schedule_host")
    if bindings.get("loop_body") is not None or host is None:
        return bindings.get("loop_body")
    from arbora.finance.loop_body import FinanceLoopBody

    return FinanceLoopBody(host)


def _with_loop_worker(
    bindings: Mapping[str, Any],
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
    session: Callable[[], Any],
) -> tuple[Mapping[str, Any], Callable[[], Coroutine[Any, Any, Any]] | None]:
    """Start the loop run worker when a ``loop_body`` is bound (ARBORA-OPS-R022.2.13).

    The worker claims the ``loop_run`` task that ``evolution.loops.run`` enqueued
    and runs the body through the bound port. With no body bound nothing is
    claimed and the queued run stays in the engine: the capability fails closed.
    """
    body = _loop_body(bindings)
    if body is None:
        return bindings, None
    import socket

    from arbora.fleet.loop_worker import LoopWorker, LoopWorkerRunner

    async def send(method: str, params: dict[str, Any]) -> Any:
        from medulla.generated import send_by_id

        client = await pool.connected_client(
            connect_context(str(session().tenant)), graph
        )
        return await send_by_id(client, method, params, graph)

    host = socket.gethostname()
    runner = LoopWorkerRunner(
        LoopWorker(send, body, worker_id=f"arbora-loops-{host}", host_iri=host),
        interval_s=float(read_env("ARBORA_LOOP_WORKER_INTERVAL_S")),
    )

    async def startup() -> None:
        runner.start()

    return {**bindings, "loop_worker_runner": runner}, startup


def _chained_startup(
    first: Callable[[], Coroutine[Any, Any, Any]] | None,
    second: Callable[[], Coroutine[Any, Any, Any]],
) -> Callable[[], Coroutine[Any, Any, Any]]:
    """Run ``first`` then ``second`` as one process start-up step."""
    if first is None:
        return second

    async def both() -> None:
        await first()
        await second()

    return both


def _identity_port(
    bindings: Mapping[str, Any] | None,
    runtime: Any,
    service_claims: Any,
    session: Callable[[], Any],
) -> Any:
    """The service identity port, or ``None`` when no administration port is bound."""
    from arbora.identity.ports import service_identity_port

    admin = (bindings or {}).get("identity_admin")
    if admin is None:
        return None
    return service_identity_port(
        admin, runtime, service_claims, lambda: str(session().tenant)
    )


def _scim_ports(identity_port: Any, store: Any = None) -> Any:
    """The SCIM ports the API application mounts, or ``None`` without a delivered credential."""
    if identity_port is None:
        return None
    from arbora.identity.ports import scim_ports_from_store

    if store is None:
        if not read_env("SECRETS_VAULT_URL"):
            return None
        from arbora.security.secrets import create_secrets_client

        store = create_secrets_client().backend
    return scim_ports_from_store(store, identity_port)


def _directory_sync(
    identity_port: Any, directory_factory: Callable[[], Any] | None, clock: Any = None
) -> Any:
    """The ``identity.directory_sync`` job (ARBORA-IDENTITY-R010.2.3.2), or ``None``."""
    if identity_port is None:
        return None
    from arbora.identity.ports import directory_sync_service, ldap_directory
    from arbora.mcp_server.background import SystemClock

    if directory_factory is None:

        def directory_factory() -> Any:
            from arbora.security.secrets import create_secrets_client

            return ldap_directory(
                base_dn=str(read_env("ARBORA_LDAP_BASE_DN") or ""),
                password=lambda: str(
                    create_secrets_client().backend.get(LDAP_BIND_CREDENTIAL_PATH) or ""
                ),
            )

    return directory_sync_service(
        read_env, identity_port, clock or SystemClock(), directory_factory
    )


#: The secret-backend path of the directory service account password.
LDAP_BIND_CREDENTIAL_PATH = "arbora/identity/ldap-bind-password"


def _scm_ingress(client: Any, graph: str) -> Any:
    """The signed code-host ingress admitter for the host's ingress slot (ARBORA-OPS-R896.8).

    The admitter exists only when ``ARBORA_DEVELOPMENT_SCM_SOURCES`` declares a source; with
    none bound, a signed delivery stays unadmitted and answers the uniform 401.
    """
    import time

    from arbora.identity.ports import scm_ingress_from_settings

    return scm_ingress_from_settings(
        client, graph, clock_ms=lambda: time.time_ns() // 1_000_000
    )


def _scm_poll_service(callers: Any, clock: Any) -> tuple[Any, ...]:
    """The code-host polling fallback, when a source is configured (ARBORA-OPS-R896.8)."""
    from arbora.api.ops.development_scm import DEFAULT_POLL_INTERVAL_S, ScmPollService
    from arbora.identity.ports import configured_scm_sources

    if not configured_scm_sources():
        return ()
    tick = float(
        str(read_env("ARBORA_DEVELOPMENT_SCM_POLL_INTERVAL_S", "") or "")
        or DEFAULT_POLL_INTERVAL_S
    )
    return (ScmPollService(callers, clock, tick),) if tick > 0 else ()


def _background_services(
    services: Any,
    runtime: Any,
    graph: str,
    session: Callable[[], Any],
    health_probes: dict[str, Callable[..., Any]],
    *,
    identity_port: Any = None,
    directory_factory: Callable[[], Any] | None = None,
    clock: Any = None,
) -> tuple[Any, ...]:
    """The host's own schedulers, each calling as the tenant's service caller.

    The curator tick is ``ARBORA_CURATOR_TICK_S`` seconds; ``0`` turns the
    scheduler off (ARBORA-OPS-R958).
    """
    from arbora.api.invoke.pipeline import invoke
    from arbora.api.invoke.service_caller import ServiceCallers
    from arbora.api.registry import Surface
    from arbora.knowledge.curator_scheduler import build_curator_scheduler
    from arbora.mcp_server.background import SystemClock

    started: list[Any] = []
    reclaim = _reclaim_service(runtime, session)
    if reclaim is not None:
        started.append(reclaim)
    push = _push_service(runtime, session)
    if push is not None:
        started.append(push)
    started.extend(
        filter(None, (_directory_sync(identity_port, directory_factory, clock),))
    )
    tick_s = float(str(read_env("ARBORA_CURATOR_TICK_S") or "300"))

    async def invoke_op(op_id: str, params: Any, caller: Any, key: str) -> Any:
        return await invoke(
            op_id,
            params,
            caller,
            Surface.MCP,
            services=services,
            idempotency_key=key,
        )

    callers = ServiceCallers(
        process_session=session, runtime=runtime, graph=graph, invoke=invoke_op
    )
    from arbora.api.domain.agent_host.usage_ledger import LEDGER

    LEDGER.configure(callers)
    started.extend(_scm_poll_service(callers, SystemClock()))
    started.extend(_tool_task_service(runtime, session, callers, health_probes))
    if tick_s <= 0:
        return tuple(started)
    return (build_curator_scheduler(callers, SystemClock(), tick_s=tick_s), *started)


def _reclaim_service(
    runtime: Any,
    session: Callable[[], Any],
    *,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> Any:
    """The A2A run reclaim consumer for this host (ARBORA-A2A-R001.2.2), or ``None``.

    It starts with the process so a run whose host died is reclaimed by a replica; it is
    absent only when the host composes no A2A service or
    ``ARBORA_A2A_RECLAIM_POLL_S`` is ``0``.
    """
    import logging
    import time

    from arbora.a2a.run_reclaim import (
        DEFAULT_POLL_INTERVAL_S,
        build_run_reclaim_service,
    )

    a2a = getattr(runtime, "bindings", {}).get("a2a")
    authority = getattr(a2a, "authority", None)
    poll = float(
        str(read_env("ARBORA_A2A_RECLAIM_POLL_S", str(DEFAULT_POLL_INTERVAL_S)) or "")
        or DEFAULT_POLL_INTERVAL_S
    )
    if authority is None or not hasattr(authority, "work_engine") or poll <= 0:
        return None
    log = logging.getLogger("arbora.a2a.reclaim")
    return build_run_reclaim_service(
        authority=authority,
        process_session=session,
        clock=clock or time.time,
        **({"sleep": sleep} if sleep is not None else {}),
        emit=lambda event, fields: log.warning("%s %s", event, dict(fields)),
        poll_interval_s=poll,
    )


def _tool_task_service(
    runtime: Any,
    session: Callable[[], Any],
    callers: Any,
    health_probes: dict[str, Callable[..., Any]],
    *,
    sleep: Callable[[float], Any] | None = None,
) -> tuple[Any, ...]:
    """The tool-task consumer for this host (ARBORA-A2A-R002.2.3.2), or none.

    One consumer starts per replica when the host composes the A2A service over an
    engine. A host that serves A2A without the engine client starts none and reports
    ``UNAVAILABLE`` on ``/ready``; a host that serves no A2A reports nothing.
    ``ARBORA_A2A_TOOL_TASK_POLL_S`` of ``0`` turns the poll off.
    """
    from arbora.a2a.tool_task_host import (
        HEALTH_PROBE,
        LazyEngine,
        build_tool_task_service,
        unavailable_probe,
    )

    a2a = getattr(runtime, "bindings", {}).get("a2a")
    if a2a is None:
        return ()
    authority = getattr(a2a, "authority", None)
    poll = float(str(read_env("ARBORA_A2A_TOOL_TASK_POLL_S") or "") or 2)
    reader = getattr(authority, "delegation_reader", None)
    if authority is None or not hasattr(authority, "engine") or reader is None:
        health_probes[HEALTH_PROBE] = unavailable_probe
        return ()
    if poll <= 0:
        return ()
    service = build_tool_task_service(
        engine=LazyEngine(authority.engine),
        tenant=str(session().tenant),
        process_session=session,
        invoke=callers.invoke,
        read_delegation=reader,
        poll_interval_s=poll,
        **({"sleep": sleep} if sleep is not None else {}),
    )
    health_probes[HEALTH_PROBE] = service.probe
    return (service,)


def _push_service(
    runtime: Any,
    session: Callable[[], Any],
    *,
    sleep: Callable[[float], Any] | None = None,
) -> Any:
    """The A2A push delivery service for this host (ARBORA-A2A-R013.2), or ``None``.

    It starts with the process so a push item survives a restart; it is absent
    only when the host composes no push worker or ``A2A_PUSH_POLL_S`` is ``0``.
    """
    import logging

    from arbora.a2a.push import DEFAULT_POLL_INTERVAL_S, PushDeliveryService

    a2a = getattr(runtime, "bindings", {}).get("a2a")
    worker = getattr(getattr(a2a, "authority", None), "push", None)
    poll = float(
        str(read_env("A2A_PUSH_POLL_S", str(DEFAULT_POLL_INTERVAL_S)) or "")
        or DEFAULT_POLL_INTERVAL_S
    )
    if worker is None or poll <= 0:
        return None
    log = logging.getLogger("arbora.a2a.push")
    return PushDeliveryService(
        worker,
        session,
        **({"sleep": sleep} if sleep is not None else {}),
        emit=lambda event, fields: log.warning("%s %s", event, dict(fields)),
        poll_interval_s=poll,
    )
