"""Experimental connection-local HTTP/SSE carrier for the ACP SDK codec.

This is not standardized ACP Streamable HTTP and provides no event replay.
The shared gateway must install its existing identity middleware. Only a
trusted application factory can compose the AU-backed agent.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from arbora.api.contracts.session import GraphSession, ScopeError, resolve_session

from .session_policy import PrincipalContext

if TYPE_CHECKING:
    from .adapter import HostedACPAgent


class _CarrierRefusal(ValueError):
    """A closed, public-safe transport refusal reason."""

    def __init__(self, reason: str, status: int = 400) -> None:
        super().__init__(reason)
        self.status = status


@dataclass(frozen=True)
class AcpHttpLimits:
    """Received limits for a transient connection, never durable replay state."""

    connections: int = 8
    frame_bytes: int = 65536
    queued_frames: int = 32
    messages: int = 128
    idle_seconds: float = 60
    lifetime_seconds: float = 600

    def __post_init__(self) -> None:
        for value in (
            self.connections,
            self.frame_bytes,
            self.queued_frames,
            self.messages,
        ):
            if type(value) is not int or value < 1:
                raise ValueError("ACP HTTP count limits must be positive integers")
        for duration in (self.idle_seconds, self.lifetime_seconds):
            if not 0 < duration <= 3600:
                raise ValueError("ACP HTTP time limits must be in (0, 3600]")


class _OutputTransport(asyncio.Transport):
    """A real StreamWriter transport backed by a bounded SSE output queue."""

    def __init__(self, connection: _Connection) -> None:
        super().__init__()
        self.connection = connection
        self.closed = False
        self.protocol: asyncio.StreamReaderProtocol | None = None

    def write(self, data: bytes | bytearray | memoryview) -> None:
        data = bytes(data)
        connection = self.connection
        if self.closed:
            raise ConnectionError("ACP connection closed")
        if len(data) > connection.limits.frame_bytes:
            self.close()
            raise ConnectionError("ACP frame limit")
        if connection.sent >= connection.limits.messages:
            self.close()
            raise ConnectionError("ACP outbound message limit")
        connection.sent += 1
        message = json.loads(data)
        if "method" in message and "id" in message:
            connection.pending.add(message["id"])
        try:
            connection.output.put_nowait(data)
        except asyncio.QueueFull:
            self.close()
            raise ConnectionError("ACP output queue limit") from None

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.connection.reader.feed_eof()
        if self.protocol is not None:
            self.protocol.connection_lost(None)
        if self.connection.output.full():
            self.connection.output.get_nowait()
        self.connection.output.put_nowait(None)

    def abort(self) -> None:
        self.close()


class _Connection:
    def __init__(
        self,
        agent: HostedACPAgent,
        principal: PrincipalContext,
        authority: list[GraphSession],
        limits: AcpHttpLimits,
        connections: dict[str, _Connection],
    ) -> None:
        self.id = uuid4().hex
        self.agent, self.principal, self.limits = agent, principal, limits
        self.authority_ref = authority
        self.connections = connections
        self.reader = asyncio.StreamReader(limit=limits.frame_bytes)
        self.output: asyncio.Queue[bytes | None] = asyncio.Queue(limits.queued_frames)
        self.transport = _OutputTransport(self)
        protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader())
        self.transport.protocol = protocol
        protocol.connection_made(self.transport)
        self.writer = asyncio.StreamWriter(
            self.transport, protocol, None, asyncio.get_running_loop()
        )
        self.pending: set[str | int] = set()
        self.seen: set[str | int] = set()
        self.count = 0
        self.sent = 0
        self.subscribed = False
        self.authorization_profile = _authorization_profile(self.authority)
        self.task = asyncio.create_task(self._serve())

    @property
    def authority(self) -> GraphSession:
        return self.authority_ref[0]

    async def _watch_authority(self) -> None:
        while True:
            self.authority.ensure_authority_current()
            await asyncio.sleep(
                min(1.0, max(0.01, _lifetime(self.authority, self.limits)))
            )

    async def _serve(self) -> None:
        import acp

        tasks: list[asyncio.Task[Any]] = []
        try:
            async with asyncio.timeout(self.limits.lifetime_seconds):
                tasks = [
                    asyncio.create_task(
                        acp.run_agent(
                            self.agent,
                            use_unstable_protocol=True,  # Pinned session/close only.
                            input_stream=self.writer,
                            output_stream=self.reader,
                            receive_timeout=self.limits.idle_seconds,
                        )
                    ),
                    asyncio.create_task(self._watch_authority()),
                ]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
        except (ConnectionError, PermissionError, TimeoutError, acp.RequestError):
            self.transport.close()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await self.agent.aclose()
            finally:
                self.transport.close()
                self.connections.pop(self.id, None)

    async def close(self) -> None:
        self.transport.close()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    def accept(self, body: bytes) -> None:
        if self.transport.closed:
            raise _CarrierRefusal("connection_closed")
        if self.count >= self.limits.messages:
            raise _CarrierRefusal("message_limit")
        message = _decode_frame(body)
        self._correlate(message)
        self.count += 1
        self.reader.feed_data(body + b"\n")

    def _correlate(self, message: dict[str, Any]) -> None:
        identifier = message.get("id")
        if "method" in message:
            if "id" in message:
                assert isinstance(identifier, (str, int))
                if identifier in self.seen:
                    raise _CarrierRefusal("duplicate_request")
                self.seen.add(identifier)
        else:
            if identifier not in self.pending or ("result" in message) == (
                "error" in message
            ):
                raise _CarrierRefusal("unexpected_response")
            self.pending.remove(identifier)

    async def events(self):
        try:
            yield b": connected\n\n"
            while (frame := await self.output.get()) is not None:
                self.authority.ensure_authority_current()
                yield b"event: message\ndata: " + frame.rstrip(b"\n") + b"\n\n"
        finally:
            with anyio.CancelScope(shield=True):
                await self.close()


def _authority(request: Request) -> GraphSession:
    if request.query_params or request.headers.get("last-event-id") is not None:
        raise _CarrierRefusal("query_and_replay_not_supported")
    session = resolve_session(required_scope="kg:write")
    if not session.actor.authenticated:
        raise PermissionError("verified identity required")
    return session


async def _body(request: Request, limit: int, timeout: float) -> bytes:
    body = bytearray()
    async with asyncio.timeout(timeout):
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise _CarrierRefusal("frame_limit")
            body.extend(chunk)
    return bytes(body)


def _decode_frame(body: bytes) -> dict[str, Any]:
    if b"\n" in body or b"\r" in body:
        raise _CarrierRefusal("single_frame_required")
    try:
        message = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _CarrierRefusal("invalid_frame") from None
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise _CarrierRefusal("invalid_frame")
    if "id" in message and type(message["id"]) not in (str, int):
        raise _CarrierRefusal("invalid_id")
    if "method" in message and (
        not isinstance(message["method"], str)
        or "result" in message
        or "error" in message
    ):
        raise _CarrierRefusal("invalid_frame")
    return message


def _lifetime(authority: GraphSession, limits: AcpHttpLimits) -> float:
    remaining = limits.lifetime_seconds
    actor = authority.actor
    expiry = (
        actor.credential_lease.expires_at
        if actor.credential_lease
        else actor.credential_expires_at
    )
    if expiry is not None:
        remaining = min(remaining, max(0.0, expiry - time.time()))
    if authority.continuity_expires_at is not None:
        remaining = min(
            remaining, max(0.0, authority.continuity_expires_at - time.monotonic())
        )
    return remaining


def _bound_principal(authority: GraphSession) -> PrincipalContext | None:
    try:
        authority.ensure_authority_current()
    except PermissionError:
        return None
    return PrincipalContext(
        authority.actor.actor_id, authority.tenant, authority.scopes
    )


def _authorization_profile(authority: GraphSession) -> tuple[Any, ...]:
    """Compare existing verified facts, not credential rotation or trace data."""
    from arbora.security.settings import security_settings

    actor, settings = authority.actor, security_settings()
    return (
        actor.actor_id,
        actor.actor_type,
        actor.tenant_id,
        actor.authenticated,
        actor.authority_mode,
        frozenset(actor.roles),
        frozenset(actor.groups),
        authority.tenant,
        authority.scopes,
        authority.graph,
        authority.policy_version,
        authority.audience,
        settings.auth_jwt_issuer,
        settings.auth_jwt_jwks_uri,
        settings.auth_jwt_audience,
        tuple(settings.auth_jwt_algorithms),
    )


class _Gateway:
    def __init__(self, app: Any, limits: AcpHttpLimits) -> None:
        self.app, self.limits = app, limits
        self.connections: dict[str, _Connection] = {}

    async def close(self) -> None:
        await asyncio.gather(
            *(connection.close() for connection in tuple(self.connections.values()))
        )

    async def create(
        self, request: Request, authority: GraphSession, principal: PrincipalContext
    ) -> Response:
        factory = getattr(self.app.state, "acp_agent_factory", None)
        if factory is None:
            raise _CarrierRefusal("chat_adapter_unavailable", 503)
        if await _body(request, self.limits.frame_bytes, self.limits.idle_seconds):
            raise _CarrierRefusal("empty_creation_body_required")
        authority.ensure_authority_current()
        # No await between final capacity check and insertion.
        if len(self.connections) >= self.limits.connections:
            raise _CarrierRefusal("connection_limit", 429)
        try:
            from .adapter import HostedACPAgent
        except ModuleNotFoundError as exc:
            if exc.name not in {
                "acp",
                "pydantic_ai_harness",
                "pydantic_ai_harness.experimental.acp",
            }:
                raise
            raise _CarrierRefusal("chat_adapter_unavailable", 503) from None
        authority_ref = [authority]
        profile = _authorization_profile(authority)
        try:
            agent = factory(
                lambda: _bound_principal(authority_ref[0]),
                request=request,
                authority=authority,
            )
            if inspect.isawaitable(agent):
                agent = await agent
            authority.ensure_authority_current()
            if _authorization_profile(authority) != profile:
                raise PermissionError("Connection authorization profile changed")
        except Exception:
            raise _CarrierRefusal("chat_adapter_unavailable", 503) from None
        if not isinstance(agent, HostedACPAgent):
            raise _CarrierRefusal("chat_adapter_unavailable", 503)
        # Async host authentication may yield; capacity must be checked again.
        if len(self.connections) >= self.limits.connections:
            await agent.aclose()
            raise _CarrierRefusal("connection_limit", 429)
        connection = _Connection(
            agent, principal, authority_ref, self.limits, self.connections
        )
        self.connections[connection.id] = connection
        return JSONResponse(
            {
                "connectionId": connection.id,
                "transport": "arbora-acp-http-sse-v1-experimental",
            },
            status_code=201,
        )

    def owned(self, identifier: str, principal: PrincipalContext) -> _Connection:
        connection = self.connections.get(identifier)
        if connection is None:
            raise _CarrierRefusal("unknown_connection", 404)
        if connection.principal != principal:
            raise _CarrierRefusal("principal_mismatch", 403)
        return connection

    async def exchange(self, request: Request, connection: _Connection) -> Response:
        if request.method == "DELETE":
            await connection.close()
            return Response(status_code=204)
        connection.authority.ensure_authority_current()
        if connection.transport.closed:
            raise _CarrierRefusal("connection_closed", 410)
        if request.method == "GET":
            if connection.subscribed:
                raise _CarrierRefusal("already_subscribed", 409)
            connection.subscribed = True
            return StreamingResponse(
                connection.events(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        if not connection.subscribed:
            raise _CarrierRefusal("event_stream_required", 409)
        body = await _body(request, self.limits.frame_bytes, self.limits.idle_seconds)
        _authority(request).ensure_authority_current()
        connection.authority.ensure_authority_current()
        connection.accept(body)
        return Response(status_code=202)

    async def endpoint(self, request: Request) -> Response:
        try:
            authority = _authority(request)
            principal = PrincipalContext(
                authority.actor.actor_id, authority.tenant, authority.scopes
            )
            identifier = request.path_params.get("connection_id")
            if identifier is None:
                return await self.create(request, authority, principal)
            connection = self.owned(identifier, principal)
            connection.authority.ensure_authority_current()
            if _authorization_profile(authority) != connection.authorization_profile:
                raise PermissionError("Connection authorization profile changed")
            await connection.agent.admit_transport_message(request, authority)
            if _authorization_profile(authority) != connection.authorization_profile:
                raise PermissionError("Connection authorization profile changed")
            connection.authority.ensure_authority_current()
            authority.ensure_authority_current()
            connection.authority_ref[0] = authority
            return await self.exchange(request, connection)
        except TimeoutError:
            return JSONResponse({"error": "request_timeout"}, status_code=408)
        except ScopeError:
            return JSONResponse({"error": "scope_required"}, status_code=403)
        except PermissionError:
            return JSONResponse({"error": "authentication_required"}, status_code=401)
        except _CarrierRefusal as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)


def register_acp_routes(
    app: Any, prefix: str = "/api", *, limits: AcpHttpLimits | None = None
) -> None:
    """Register the experimental carrier with fail-closed host composition.

    ``acp_agent_factory(principal_provider, request=..., authority=...)`` returns
    HostedACPAgent, optionally awaited. Only the host authenticates the request.
    Every carrier request independently resolves and matches verified identity;
    same-identity verified bearer renewal refreshes the connection authority.
    A closed/expired connection cannot be resumed; absolute lifetime stays bounded.
    Existing application lifespan is preserved and closes all connections.
    ``app.state.acp_close_connections()`` also permits explicit host teardown.
    """
    gateway = _Gateway(app, limits or AcpHttpLimits())
    app.state.acp_close_connections = gateway.close
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: Any):
        async with previous_lifespan(application) as state:
            try:
                yield state
            finally:
                await gateway.close()

    app.router.lifespan_context = lifespan
    app.add_route(f"{prefix}/acp/connections", gateway.endpoint, methods=["POST"])
    app.add_route(
        f"{prefix}/acp/connections/{{connection_id}}",
        gateway.endpoint,
        methods=["GET", "POST", "DELETE"],
    )
