"""The SDK A2A application, served behind Graph OS admission.

Graph OS builds the A2A protocol application with the SDK factory
(``agent_connector_sdk.a2a.create_a2a_app``) and serves it behind a guard. The
guard verifies the caller before any request reaches the application. A read
(``GET`` or ``HEAD``) needs the read scope. Any other method needs the write
scope. A refused or unverified caller gets HTTP 401 with a JSON body, and the
application never sees the request. The guard runs the application inside
the caller's binding, so each tool call carries the verified caller.

The agent comes from the composition root. Its toolset is the multiplexer,
so every tool call that an A2A task makes goes through the multiplexer and
its admission.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any, Protocol

from agent_connector_sdk.a2a.models import AgentSkill

__all__ = ["AdmittedA2AApp", "CallerAuthenticator", "create_admitted_a2a_app"]

READ_SCOPE = "kg:read"
WRITE_SCOPE = "kg:write"
_READ_METHODS = frozenset({"GET", "HEAD"})

Scope = dict[str, Any]
Receive = Callable[[], Any]
Send = Callable[[dict[str, Any]], Any]
CallerBinding = Callable[[Any], AbstractContextManager[Any]]


class CallerAuthenticator(Protocol):
    """Verifies the caller of one request for one scope."""

    async def authenticate(self, request: Any, *, scope: str) -> Any:
        """Return the verified caller, or ``None``; raise ``PermissionError`` to refuse."""


async def _refuse(send: Send) -> None:
    body = json.dumps({"error": "A2A authority required"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class AdmittedA2AApp:
    """An ASGI guard that admits each HTTP request before the A2A application."""

    def __init__(
        self,
        app: Any,
        authenticator: CallerAuthenticator,
        *,
        bind_caller: CallerBinding | None = None,
        read_scope: str = READ_SCOPE,
        write_scope: str = WRITE_SCOPE,
    ) -> None:
        self.app = app
        self._authenticator = authenticator
        self._bind = bind_caller or (lambda _caller: nullcontext())
        self._read_scope = read_scope
        self._write_scope = write_scope

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        from starlette.requests import Request

        method = str(scope.get("method", "GET")).upper()
        required = self._read_scope if method in _READ_METHODS else self._write_scope
        try:
            caller = await self._authenticator.authenticate(
                Request(scope, receive), scope=required
            )
        except PermissionError:
            caller = None
        if caller is None:
            await _refuse(send)
            return
        with self._bind(caller):
            await self.app(scope, receive, send)


def create_admitted_a2a_app(
    agent: Any,
    *,
    authenticator: CallerAuthenticator,
    name: str,
    url: str,
    version: str,
    storage: Any,
    broker: Any,
    description: str = "",
    skills: list[AgentSkill] | None = None,
    bind_caller: CallerBinding | None = None,
) -> AdmittedA2AApp:
    """Build the SDK A2A application for ``agent`` and wrap it in the guard."""
    from agent_connector_sdk.a2a import create_a2a_app

    app = create_a2a_app(
        agent,
        name=name,
        url=url,
        version=version,
        description=description,
        skills=skills,
        storage=storage,
        broker=broker,
    )
    return AdmittedA2AApp(app, authenticator, bind_caller=bind_caller)
