"""The SDK A2A application, served behind Arbora admission.

Arbora builds the A2A protocol application with the SDK factory
(``scion_sdk.a2a.create_a2a_app``) and serves it behind a guard. The
guard verifies the caller before any request reaches the application. A read
(``GET`` or ``HEAD``) needs the read scope. Any other method needs the write
scope. A refused or unverified caller gets HTTP 401 with a JSON body, and the
application never sees the request. The guard runs the application inside
the caller's binding, so each tool call carries the verified caller.

``message/stream`` and ``tasks/resubscribe`` never reach the SDK application.
The guard serves them through the one A2A JSON-RPC handler and its durable event
log (ARBORA-A2A-R001.4.1), so Arbora has a single streaming path. With no stream
handler wired, both answer the typed ``-32010`` refusal with HTTP 501
(ARBORA-A2A-R001.4); the SDK application never answers a stream method.

``tasks/list`` never reaches the SDK application either: the guard answers it from
the caller's own conversations (``list_contexts`` of the engine storage, newest first,
ARBORA-A2A-R001.3). A request that names an absent or foreign ``contextId`` is
refused with JSON-RPC ``-32001`` whatever the SDK application did with it: the
guard maps :class:`~arbora.a2a.storage.A2AContextNotFound` to that one error.

The agent comes from the composition root. Its toolset is the multiplexer,
so every tool call that an A2A task makes goes through the multiplexer and
its admission.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any, Protocol

from scion_sdk.a2a.models import AgentSkill

__all__ = ["AdmittedA2AApp", "CallerAuthenticator", "create_admitted_a2a_app"]

READ_SCOPE = "kg:read"
WRITE_SCOPE = "kg:write"
_READ_METHODS = frozenset({"GET", "HEAD"})

Scope = dict[str, Any]
Receive = Callable[[], Any]
Send = Callable[[dict[str, Any]], Any]
CallerBinding = Callable[[Any], AbstractContextManager[Any]]
#: The shared A2A JSON-RPC handler: a Starlette request in, a response out.
StreamHandler = Callable[[Any], Awaitable[Any]]
#: ``list_contexts(limit=, cursor=)`` of the caller's engine storage.
ListContexts = Callable[..., Awaitable[Any]]
LIST_METHOD = "tasks/list"
CONTEXT_NOT_FOUND = -32001
STREAM_METHODS = frozenset({"message/stream", "tasks/resubscribe"})
#: A request body larger than this is not inspected for a stream method.
_MAX_PEEK_BYTES = 4 * 1024 * 1024


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


async def _stream_refusal(send: Send, request_id: Any) -> None:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32010, "message": "streaming is not available"},
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 501,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _json_response(send: Send, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


async def _read_body(receive: Receive) -> tuple[bytes, bool]:
    """The request body, and whether it was read whole within the peek bound."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return b"".join(chunks), False
        chunks.append(message.get("body", b""))
        size += len(chunks[-1])
        if size > _MAX_PEEK_BYTES:
            return b"".join(chunks), False
        if not message.get("more_body", False):
            return b"".join(chunks), True


def _envelope(body: bytes) -> dict[str, Any]:
    try:
        envelope = json.loads(body)
    except ValueError:
        return {}
    return envelope if isinstance(envelope, dict) else {}


def _page_params(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """The ``limit`` and ``cursor`` of a ``tasks/list`` request, or ``None`` if invalid."""
    params = envelope.get("params") or {}
    if not isinstance(params, dict) or set(params) - {"limit", "cursor"}:
        return None
    limit, cursor = params.get("limit"), params.get("cursor")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)):
        return None
    if cursor is not None and not isinstance(cursor, str):
        return None
    page: dict[str, Any] = {"cursor": cursor}
    if limit is not None:
        page["limit"] = limit
    return page


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
        stream_handler: StreamHandler | None = None,
        list_contexts: ListContexts | None = None,
    ) -> None:
        self.app = app
        self._stream_handler = stream_handler
        self._list_contexts = list_contexts
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
            if method != "POST":
                await self.app(scope, receive, send)
                return
            await self._post(scope, receive, send)

    async def _post(self, scope: Scope, receive: Receive, send: Send) -> None:
        body, whole = await _read_body(receive)
        replayed = False

        async def replay() -> dict[str, Any]:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        envelope = _envelope(body) if whole else {}
        await self._route(scope, replay, send, envelope)

    async def _route(
        self, scope: Scope, replay: Receive, send: Send, envelope: dict[str, Any]
    ) -> None:
        from starlette.requests import Request

        stream = (
            envelope.get("method") if envelope.get("method") in STREAM_METHODS else None
        )
        request_id = envelope.get("id")
        if stream is None and envelope.get("method") == LIST_METHOD:
            await self._list(envelope, send)
        elif stream is None:
            await self._guarded(scope, replay, send, envelope.get("id"))
        elif self._stream_handler is None:
            await _stream_refusal(send, request_id)
        else:
            response = await self._stream_handler(Request(scope, replay))
            await response(scope, replay, send)

    async def _list(self, envelope: dict[str, Any], send: Send) -> None:
        """``tasks/list``: the verified caller's conversations, newest first."""
        request_id = envelope.get("id")
        page = _page_params(envelope)
        if self._list_contexts is None:
            await _json_response(
                send, 404, _rpc_error(request_id, -32601, "Method not found")
            )
        elif page is None:
            await _json_response(
                send, 400, _rpc_error(request_id, -32602, "Invalid params")
            )
        else:
            result = await self._list_contexts(**page)
            await _json_response(
                send, 200, {"jsonrpc": "2.0", "id": request_id, "result": result}
            )

    async def _guarded(
        self, scope: Scope, receive: Receive, send: Send, request_id: Any
    ) -> None:
        """Run the SDK application; an absent or foreign context answers ``-32001``.

        The reply is held until the application returns, so a refusal raised inside
        it replaces whatever the application had begun to send.
        """
        from .storage import A2AContextNotFound

        held: list[dict[str, Any]] = []

        async def hold(message: dict[str, Any]) -> None:
            held.append(message)

        try:
            await self.app(scope, receive, hold)
        except A2AContextNotFound:
            await _json_response(
                send,
                404,
                _rpc_error(request_id, CONTEXT_NOT_FOUND, "Context not found"),
            )
            return
        for message in held:
            await send(message)


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
    stream_handler: StreamHandler | None = None,
    list_contexts: ListContexts | None = None,
) -> AdmittedA2AApp:
    """Build the SDK A2A application for ``agent`` and wrap it in the guard."""
    from scion_sdk.a2a import create_a2a_app

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
    return AdmittedA2AApp(
        app,
        authenticator,
        bind_caller=bind_caller,
        stream_handler=stream_handler,
        list_contexts=list_contexts,
    )
