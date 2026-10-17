"""Explicit production sink composition; grants remain externally owned."""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Literal

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from arbora.agent_host.log_access import HostLogAccess, WorkspaceGrantProvider
from arbora.agent_host.log_routes import host_log_credential
from arbora.agent_host.log_sink import WorkspaceLogSink
from arbora.identity.ports import SessionAuthority, SessionState


@dataclass(frozen=True)
class HostLogRuntime:
    """Borrowed runtime resources; the creating composition owns shutdown."""

    sessions: SessionAuthority
    sink: WorkspaceLogSink
    access: HostLogAccess

    @classmethod
    def compose(
        cls,
        *,
        sessions: SessionAuthority,
        grants: WorkspaceGrantProvider,
        workspace: str,
        interface_mode: Literal["terminal", "web", "both"],
        web_logs: bool,
    ) -> "HostLogRuntime":
        sink = WorkspaceLogSink(workspace, uuid.uuid4().hex)
        try:
            access = HostLogAccess(
                sessions=sessions,
                grants=grants,
                source=sink,
                workspace=sink.workspace,
                host_instance=sink.host_instance,
                interface_mode=interface_mode,
                web_logs=web_logs,
            )
        except BaseException:
            sink.close()
            raise
        return cls(sessions, sink, access)


def register_host_log_runtime(app: FastAPI, runtime: HostLogRuntime) -> None:
    """Record authenticated request arrival before invoking the host handler.

    This optional composition requires an exact session for host requests. It
    does not grant agent actions; existing handler authorization still applies.
    No body, query string, caller header or credential enters the sink.
    """

    @app.middleware("http")
    async def record_arrival(request: Request, call_next):
        if request.url.path.startswith("/host/logs/"):
            return await call_next(request)
        try:
            async with asyncio.timeout(1):
                credential = host_log_credential(request)
                state = await runtime.sessions.resolve_session(credential)
                if not isinstance(state, SessionState):
                    raise PermissionError("workspace_denied")
                # Validate current expiry through the identity owner's public fact.
                import time

                if state.expires_at_ms <= int(time.time() * 1000):
                    raise PermissionError("workspace_denied")
                await runtime.sink.emit(
                    tenant=str(state.resolution.request_context["tenant"]),
                    request_id=uuid.uuid4().hex,
                    message="Authenticated host request received",
                )
        except Exception:  # noqa: BLE001 - required sink and authority fail before dispatch
            return JSONResponse({"detail": "log_unavailable"}, status_code=503)
        return await call_next(request)
