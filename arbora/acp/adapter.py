"""Bounded ACP wire projection; AU alone executes the admitted run."""

from __future__ import annotations

import asyncio
import inspect
import posixpath
from collections.abc import Awaitable, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import Any, NoReturn
from uuid import uuid4

import acp
from acp import schema
from meristem.harness.hosted_run import iterate_client_run
from meristem.host_ports import ClientCapabilityRefused
from meristem.runtime import RunRequest
from pydantic_ai.messages import (
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
)
from pydantic_ai_harness.experimental.acp import default_coding_presenter

from .coding_session import (
    ClientCapability,
    CodingSessionAdmission,
    CodingSessionAdmitted,
    CodingSessionRequest,
)
from .session_policy import (
    AcpRefusal,
    AcpSessionAdmission,
    AcpSessionPolicy,
    PrincipalContext,
)

RequestFactory = Callable[
    [PrincipalContext, str, str], RunRequest | Awaitable[RunRequest]
]


@dataclass
class _Session:
    admission: CodingSessionAdmitted
    active: asyncio.Task[schema.PromptResponse] | None = None
    output_bytes: int = 0


def _validate_session_options(
    cwd: str, additional_directories: list[str] | None, mcp_servers: list[Any] | None
) -> None:
    if not cwd.startswith("/") or "\\" in cwd or "\x00" in cwd:
        raise acp.RequestError.invalid_params({"reason": "unsupported_workspace_path"})
    if mcp_servers or additional_directories:
        raise acp.RequestError.invalid_params(
            {"reason": "unsupported_session_capability"}
        )


def _prompt_text(prompt: list[Any], max_bytes: int) -> str:
    if not prompt or any(
        not isinstance(part, schema.TextContentBlock) for part in prompt
    ):
        raise acp.RequestError.invalid_params({"reason": "text_prompt_required"})
    text = "\n".join(part.text for part in prompt)
    if len(text.encode()) > max_bytes:
        raise acp.RequestError.invalid_params({"reason": "prompt_limit"})
    return text


def _normalize_read_path(params: dict[str, Any], root: str) -> None:
    if "path" in params:
        path = params["path"]
        if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
            raise ClientCapabilityRefused("permission_denied")
        absolute = posixpath.normpath(posixpath.join(root, path))
        if posixpath.commonpath((root, absolute)) != root:
            raise ClientCapabilityRefused("permission_denied")
        params["path"] = absolute


class HostedACPAgent:
    """Connection-local protocol state, never an agent loop or durable store.

    ``principal`` must resolve host-verified authority, not ACP payload fields.
    ``request_factory`` must return a host-admitted request including any history
    binding. It is deliberately absent by default, so no execution is implied.
    """

    def __init__(
        self,
        principal: Callable[[], PrincipalContext | None],
        *,
        policy: AcpSessionPolicy,
        trusted_roots: frozenset[str],
        request_factory: RequestFactory | None = None,
        hosted_turn: Callable[
            [str, str, Callable[[Any], Awaitable[None]]], Awaitable[None]
        ]
        | None = None,
        completed: Callable[[RunRequest, dict[str, Any]], Awaitable[None]]
        | None = None,
        closed: Callable[[str], Awaitable[None]] | None = None,
        transport_admission: Callable[[Any, Any], Awaitable[None]] | None = None,
        max_sessions: int = 16,
        max_prompt_bytes: int = 65536,
        max_output_bytes: int = 1_048_576,
    ) -> None:
        if any(
            type(value) is not int or value < 1
            for value in (max_sessions, max_prompt_bytes, max_output_bytes)
        ):
            raise ValueError("ACP limits must be positive integers")
        if hosted_turn is not None and (
            request_factory is not None or completed is not None
        ):
            raise ValueError("Choose one host execution authority")
        self._principal = principal
        self._factory = request_factory
        self._hosted_turn = hosted_turn
        self._completed = completed
        self._closed = closed
        self._transport_admission = transport_admission
        self._harness_available = (
            "stream_observer" in inspect.signature(iterate_client_run).parameters
        )
        self._policy = AcpSessionAdmission(policy)
        self._admission = CodingSessionAdmission(policy, trusted_roots=trusted_roots)
        self._max_sessions = max_sessions
        self._max_prompt_bytes = max_prompt_bytes
        self._max_output_bytes = max_output_bytes
        self._sessions: dict[str, _Session] = {}
        self._client: acp.Client | None = None
        self._capabilities: frozenset[ClientCapability] = frozenset()
        self._initialized = False
        self._closing = False

    async def admit_transport_message(self, request: Any, authority: Any) -> None:
        """Trusted carrier hook; protocol bodies never establish HTTP identity."""
        if self._transport_admission is not None:
            await self._transport_admission(request, authority)

    def on_connect(self, conn: acp.Client) -> None:
        if self._client is not None or self._initialized or self._closing:
            raise RuntimeError("ACP adapter instances cannot be rebound")
        self._client = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: schema.ClientCapabilities | None = None,
        client_info: schema.Implementation | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse:
        del client_info  # Client display metadata conveys no authority or capability.
        if self._initialized:
            raise acp.RequestError.invalid_request({"reason": "already_initialized"})
        if self._principal() is None:
            raise acp.RequestError.auth_required()
        self._initialized = True
        fs = client_capabilities.fs if client_capabilities else None
        capabilities = set()
        if fs and fs.read_text_file:
            capabilities.add(ClientCapability.READ_TEXT_FILE)
        if fs and fs.write_text_file:
            capabilities.add(ClientCapability.WRITE_TEXT_FILE)
        if client_capabilities and client_capabilities.terminal:
            capabilities.add(ClientCapability.TERMINAL_CREATE)
        self._capabilities = frozenset(capabilities)
        return schema.InitializeResponse(
            protocol_version=1,
            agent_info=schema.Implementation(name="arbora", version="experimental"),
            agent_capabilities=schema.AgentCapabilities(
                load_session=False,
                session_capabilities=schema.SessionCapabilities(
                    close=schema.SessionCloseCapabilities()
                ),
            ),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.NewSessionResponse:
        if not self._initialized or self._client is None or self._closing:
            raise acp.RequestError.invalid_request({"reason": "not_initialized"})
        _validate_session_options(cwd, additional_directories, mcp_servers)
        if len(self._sessions) >= self._max_sessions:
            raise acp.RequestError(-32001, "Session limit reached")
        try:
            admitted = self._admission.admit(
                self._principal(),
                CodingSessionRequest(cwd, self._capabilities),
                chat_adapter_available=self._hosted_turn is not None
                or (self._factory is not None and self._harness_available),
            )
        except AcpRefusal as exc:
            raise acp.RequestError(
                -32001, "Session refused", {"reason": exc.reason.value}
            ) from None
        session_id = uuid4().hex
        self._sessions[session_id] = _Session(admitted)
        return schema.NewSessionResponse(session_id=session_id)

    def _session(
        self, session_id: str, *, teardown: bool = False, touch: bool = True
    ) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise acp.RequestError.invalid_params({"reason": "unknown_session"})
        if teardown:
            if self._principal() != session.admission.snapshot.principal:
                raise acp.RequestError.auth_required()
            return session
        try:
            snapshot = self._policy.check_bound_message(
                session.admission.snapshot,
                self._principal(),
                chat_adapter_available=self._hosted_turn is not None
                or (self._factory is not None and self._harness_available),
            )
        except AcpRefusal as exc:
            raise acp.RequestError(
                -32001, "Session refused", {"reason": exc.reason.value}
            ) from None
        if touch:
            session.admission = replace(session.admission, snapshot=snapshot)
        return session

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **kwargs: Any,
    ) -> schema.PromptResponse:
        session = self._session(session_id, touch=False)
        if session.active is not None:
            raise acp.RequestError(-32001, "Session already running")
        text = _prompt_text(prompt, self._max_prompt_bytes)
        self._session(session_id)
        session.output_bytes = 0
        session.active = asyncio.create_task(self._run(session_id, session, text))
        try:
            return await session.active
        except asyncio.CancelledError:
            # Protocol dispatcher teardown must propagate cancellation; only a
            # separately cancelled AU task is an ACP session/cancel response.
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                raise
            return schema.PromptResponse(stop_reason="cancelled")
        except acp.RequestError:
            raise
        except Exception:
            raise acp.RequestError(-32001, "Run failed") from None
        finally:
            session.active = None

    async def _run(
        self, session_id: str, session: _Session, text: str
    ) -> schema.PromptResponse:
        async def observe(event: Any) -> None:
            chunk = ""
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                chunk = event.part.content
            elif isinstance(event, PartDeltaEvent) and isinstance(
                event.delta, TextPartDelta
            ):
                chunk = event.delta.content_delta
            if chunk:
                assert self._client is not None
                for offset in range(0, len(chunk), 4096):
                    await self._send_update(
                        session_id,
                        acp.update_agent_message_text(chunk[offset : offset + 4096]),
                    )

        if self._hosted_turn is not None:
            self._session(session_id, touch=False)
            await self._hosted_turn(session_id, text, observe)
            self._session(session_id, touch=False)
            return schema.PromptResponse(stop_reason="end_turn")

        assert self._factory is not None
        request = self._factory(session.admission.snapshot.principal, session_id, text)
        if inspect.isawaitable(request):
            request = await request
        self._session(session_id)
        if not isinstance(request, RunRequest) or request.task != text:
            raise acp.RequestError(-32001, "Host request refused")

        transport = _ReadTransport(self, session_id)
        async with aclosing(
            iterate_client_run(request, transport, stream_observer=observe)
        ) as run:
            async for event in run:
                if event["type"] == "error":
                    raise acp.RequestError(-32001, "Run failed")
                if event["type"] == "graph_complete":
                    if event["status"] != "completed":
                        raise acp.RequestError(-32001, "Run stopped")
                    self._session(session_id, touch=False)
                    if self._completed is not None:
                        await self._completed(request, event["result"])
                    return schema.PromptResponse(stop_reason="end_turn")
        raise acp.RequestError(-32001, "Run incomplete")

    async def _send_update(self, session_id: str, update: Any) -> None:
        session = self._session(session_id, touch=False)
        if self._client is None:
            raise ClientCapabilityRefused("client_disconnected")
        size = len(update.model_dump_json().encode()) + len(session_id) + 128
        if session.output_bytes + size > self._max_output_bytes:
            raise ClientCapabilityRefused("output_limit")
        session.output_bytes += size
        await self._client.session_update(session_id, update)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        session = self._session(session_id, teardown=True)
        if session.active is not None:
            session.active.cancel()

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> schema.CloseSessionResponse:
        session = self._session(session_id, teardown=True)
        self._sessions.pop(session_id)
        if session.active is not None:
            session.active.cancel()
            await asyncio.gather(session.active, return_exceptions=True)
        try:
            if self._closed is not None:
                await self._closed(session_id)
        finally:
            self._sessions.pop(session_id, None)
        return schema.CloseSessionResponse()

    async def aclose(self) -> None:
        self._closing = True
        sessions, self._sessions = self._sessions, {}
        tasks = [s.active for s in sessions.values() if s.active is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if self._closed is not None:
                outcomes = await asyncio.gather(
                    *(self._closed(key) for key in sessions),
                    return_exceptions=True,
                )
                for outcome in outcomes:
                    if isinstance(outcome, BaseException):
                        raise outcome
        finally:
            self._sessions.clear()
            self._client = None

    async def load_session(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise acp.RequestError.method_not_found("session/load")

    async def list_sessions(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise acp.RequestError.method_not_found("session/list")

    async def authenticate(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise acp.RequestError.method_not_found("authenticate")

    async def set_session_mode(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise acp.RequestError.method_not_found("session/set_mode")

    async def set_config_option(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise acp.RequestError.method_not_found("session/set_config_option")

    async def fork_session(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise acp.RequestError.method_not_found("session/fork")

    async def resume_session(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise acp.RequestError.method_not_found("session/resume")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise acp.RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        raise acp.RequestError.method_not_found(method)


class _ReadTransport:
    def __init__(self, agent: HostedACPAgent, session_id: str) -> None:
        self._agent = agent
        self._session_id = session_id

    async def call(self, method: str, params: Mapping[str, Any]) -> str:
        agent = self._agent
        session = agent._session(self._session_id)
        params = dict(params)
        _normalize_read_path(params, session.admission.request.workspace_root)
        try:
            session.admission = agent._admission.check_capability(
                session.admission,
                method,
                client_connected=agent._client is not None,
                chat_adapter_available=agent._factory is not None,
            )
        except AcpRefusal as exc:
            if exc.reason.value == "permission_required":
                await self._present(method, params, "failed")
            raise ClientCapabilityRefused(exc.reason.value) from None
        assert agent._client is not None
        call_id = await self._present(method, params, "in_progress")
        try:
            agent._session(self._session_id, touch=False)
            response = await agent._client.read_text_file(self._session_id, **params)
        except asyncio.CancelledError:
            raise
        except Exception:
            await agent._send_update(
                self._session_id, acp.update_tool_call(call_id, status="failed")
            )
            raise
        await agent._send_update(
            self._session_id, acp.update_tool_call(call_id, status="completed")
        )
        return response.content

    async def _present(self, method: str, params: dict[str, Any], status: Any) -> str:
        names = {
            "fs/read_text_file": "read_file",
            "fs/write_text_file": "write_file",
            "terminal/create": "run_command",
        }
        call_id = uuid4().hex
        presentation = default_coding_presenter(
            ToolCallPart(names[method], params, tool_call_id=call_id)
        )
        assert self._agent._client is not None
        await self._agent._send_update(
            self._session_id,
            acp.start_tool_call(
                call_id,
                "Read client file"
                if status == "in_progress"
                else "Preview only: permission required",
                kind=presentation.kind if presentation else None,
                status=status,
                content=presentation.content if presentation else None,
                locations=presentation.locations if presentation else None,
            ),
        )
        return call_id


async def serve_acp_stdio(agent: HostedACPAgent) -> None:
    """Serve an explicitly composed host; no implicit local or remote fallback."""
    from .stdio_transport import serve

    await serve(agent)
