"""Authenticated JSON-RPC/SSE duplex sessions on the shared gateway."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from arbora.api.http.auth import (
    AmbientHTTPAuthenticator,
    verified_bearer_session_for_request,
)
from arbora.api.invoke.bootstrap import verify_current_from_session
from arbora.api.invoke.steps import VerifiedCaller, authenticate

from . import run_adapter
from .coding_session import CodingSessionAdmission, CodingSessionRequest
from .duplex import MAX_MESSAGE_BYTES, ClientChannel
from .session_policy import AcpRefusal, AcpSessionPolicy, PrincipalContext

SESSION_HEADER = "Arbora-Acp-Session"


async def _current(caller: VerifiedCaller) -> None:
    current = await verify_current_from_session(caller)
    if current != caller or authenticate(current) is not None:
        raise PermissionError("ACP authority changed")


class AcpRoutes:
    """Ephemeral channels; authenticated owner state remains in GraphSession."""

    def __init__(self, admission: CodingSessionAdmission, authenticator: Any) -> None:
        self.admission = admission
        self.authenticator = authenticator
        self.channels: dict[str, tuple[VerifiedCaller, ClientChannel]] = {}

    async def caller(self, request: Request) -> VerifiedCaller:
        caller = await self.authenticator.authenticate(request)
        if (
            authenticate(caller) is not None
            or "kg:write" not in caller.effective_scopes
        ):
            raise PermissionError("ACP caller required")
        await _current(caller)
        return caller

    def channel(self, request: Request, caller: VerifiedCaller) -> ClientChannel:
        entry = self.channels.get(request.headers.get(SESSION_HEADER, ""))
        if entry is None or replace(entry[0], request_id="") != replace(
            caller, request_id=""
        ):
            raise PermissionError("ACP connection unavailable")
        channel = entry[1]
        try:
            self.admission.check_session(channel.session)
        except AcpRefusal:
            channel.close()
        if channel.closed:
            self.channels.pop(channel.session_id, None)
            raise PermissionError("ACP connection closed")
        return channel

    async def post(self, request: Request) -> JSONResponse:
        request_id: Any = None
        try:
            caller = await self.caller(request)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_MESSAGE_BYTES:
                    raise ValueError("ACP request limit")
            message = json.loads(body)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError("Invalid ACP message")
            candidate_id = message.get("id")
            if not isinstance(candidate_id, (str, int)) or isinstance(
                candidate_id, bool
            ):
                raise ValueError("ACP request id required")
            request_id = candidate_id
            result = await self.dispatch(request, caller, message)
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})
        except (PermissionError, AcpRefusal, ValueError, KeyError, TypeError) as exc:
            reason = (
                exc.reason.value if isinstance(exc, AcpRefusal) else "request_refused"
            )
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32001, "message": reason},
                },
                status_code=400,
            )

    async def dispatch(
        self, request: Request, caller: VerifiedCaller, message: dict[str, Any]
    ) -> Any:
        params = message.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("Invalid ACP parameters")
        method = message.get("method")
        if method == "session/new":
            return self.open(caller, message.get("params", {}))
        channel = self.channel(request, caller)
        if method is None:
            channel.reply(message)
            return {}
        params = message.get("params", {})
        if params.get("sessionId") != channel.session_id:
            raise PermissionError("ACP session mismatch")
        return await self.session_message(channel, caller, method, params)

    async def session_message(
        self,
        channel: ClientChannel,
        caller: VerifiedCaller,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        if method == "session/close":
            channel.close()
            self.channels.pop(channel.session_id, None)
            return {}
        if method == "session/cancel":
            if channel.run_task is not None:
                channel.run_task.cancel()
            return {}
        if method != "session/prompt" or not isinstance(params.get("prompt"), str):
            raise ValueError("Unsupported ACP method")
        if not channel.connected or channel.prompt_lock.locked():
            raise ValueError("ACP connection unavailable or busy")
        async with channel.prompt_lock:
            return await self.prompt(channel, caller, params["prompt"])

    async def prompt(
        self, channel: ClientChannel, caller: VerifiedCaller, text: str
    ) -> Any:
        from arbora.api.contracts.session import use_session
        from arbora.security.actor_context import use_actor

        with use_actor(caller.session.actor), use_session(caller.session):
            channel.run_task = asyncio.create_task(
                run_adapter.run_prompt(channel, text)
            )
        try:
            return await channel.run_task
        except asyncio.CancelledError:
            return {"stopReason": "cancelled"}
        finally:
            channel.run_task = None

    def open(self, caller: VerifiedCaller, params: dict[str, Any]) -> dict[str, str]:
        for _, channel in self.channels.values():
            try:
                self.admission.check_session(channel.session)
            except AcpRefusal:
                channel.close()
        self.channels = {
            key: item for key, item in self.channels.items() if not item[1].closed
        }
        if len(self.channels) >= 64:
            raise ValueError("ACP connection limit")
        if set(params) != {"cwd", "clientCapabilities"}:
            raise ValueError("Invalid ACP admission parameters")
        session = self.admission.admit(
            PrincipalContext(caller.principal, caller.tenant, caller.effective_scopes),
            CodingSessionRequest(
                params["cwd"], frozenset(params["clientCapabilities"])
            ),
            chat_adapter_available=run_adapter.available(),
        )
        session_id = uuid4().hex
        channel = ClientChannel(
            session_id, session, self.admission, lambda: _current(caller)
        )
        self.channels[session_id] = caller, channel
        return {"sessionId": session_id}

    async def events(self, request: Request) -> Any:
        try:
            caller = await self.caller(request)
            channel = self.channel(request, caller)
            if request.headers.get("last-event-id") or channel.connected:
                raise ValueError("ACP replay unavailable")
            channel.connected = True
            return StreamingResponse(
                channel.events(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store"},
            )
        except (PermissionError, ValueError, AcpRefusal):
            return JSONResponse({"error": "connection_unavailable"}, status_code=403)


def register_acp_routes(
    app: Any,
    prefix: str = "/api",
    *,
    admission: CodingSessionAdmission | None = None,
    authenticator: Any = None,
) -> AcpRoutes:
    from arbora.core.settings import runtime_config

    if admission is None:
        admission = CodingSessionAdmission(
            AcpSessionPolicy(
                timedelta(seconds=runtime_config.acp_idle_seconds),
                timedelta(seconds=runtime_config.acp_absolute_seconds),
            ),
            trusted_roots=frozenset(runtime_config.acp_trusted_roots),
        )
    routes = AcpRoutes(
        admission,
        authenticator
        or AmbientHTTPAuthenticator(
            session_for_request=verified_bearer_session_for_request,
        ),
    )
    app.add_route(f"{prefix}/acp", routes.post, methods=["POST"])
    app.add_route(f"{prefix}/acp/events", routes.events, methods=["GET"])
    return routes
