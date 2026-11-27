"""Terminal-mode host launch and shutdown (ARBORA-HOST-R009.2, R009.4.2).

The host serves HTTP in the ``arbora-host-http`` thread while the owner's terminal client
runs in the foreground. When the terminal exits, the ``finally`` block drains the log sink
and stops the one thread this launch created; a service the supervisor only borrowed keeps
running.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from arbora.agent_host.start_admission import AdmittedHostStart, HostStartContext
from arbora.agent_host.start_configuration import (
    HostRuntimeResources,
    HostStartConfiguration,
)
from arbora.agent_host.terminal_http import TerminalHttpHosts

__all__ = ["run_terminal_host"]


def run_terminal_host(
    *,
    context: HostStartContext,
    admitted: AdmittedHostStart,
    configuration: HostStartConfiguration,
    runtime: HostRuntimeResources,
    supervisor: Any | None = None,
    ready_timeout: float = 10.0,
) -> None:
    """Serve HTTP in the named thread, run the terminal client, then stop what we started."""
    from arbora.agent_host import build_agent_host_app
    from arbora.mcp_server.composition import CoServiceSupervisor

    client = runtime.terminal_client
    if not callable(client):
        raise ValueError("terminal_client_required")
    owner = supervisor if supervisor is not None else CoServiceSupervisor()
    hosts = TerminalHttpHosts(owner)
    log_runtime = runtime.host_log_runtime
    host_instance = (
        log_runtime.sink.host_instance if log_runtime is not None else uuid.uuid4().hex
    )
    app = build_agent_host_app(
        graph=runtime.graph,
        graph_config=runtime.graph_config,
        name=configuration.name,
        description=configuration.system_prompt,
        executor=runtime.executor,
        host_logs=runtime.host_logs,
        host_log_runtime=log_runtime,
        host_start=context,
        host_configuration=configuration,
        host_runtime=runtime,
    )
    host = hosts.start(
        host_instance=host_instance,
        app=app,
        session=admitted.graph_session,
        authority=runtime.engine,
        bind_host=configuration.host or "127.0.0.1",
        port=9000 if configuration.port is None else configuration.port,
        ready_timeout=ready_timeout,
    )
    try:
        client(host, admitted)
    finally:
        try:
            if log_runtime is not None:
                asyncio.run(log_runtime.sink.drain())
        finally:
            hosts.stop(host_instance)
