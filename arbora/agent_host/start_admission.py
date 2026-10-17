"""Injected startup policy and exact owner-attested session binding."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from arbora.identity.ports import SessionAuthority, SessionState

if TYPE_CHECKING:
    from arbora.api.contracts.session import GraphSession


@dataclass(frozen=True)
class HostStartRequest:
    workspace: str
    interfaces: Literal["terminal", "web", "both"]
    web_logs: bool
    configuration_digest: str

    def __post_init__(self) -> None:
        if (
            self.interfaces not in {"terminal", "web", "both"}
            or type(self.web_logs) is not bool
            or not re.fullmatch(r"[0-9a-f]{64}", self.configuration_digest)
        ):
            raise ValueError("invalid_launch")
        path = Path(self.workspace).resolve(strict=True)
        if not path.is_dir():
            raise ValueError("invalid_launch")
        object.__setattr__(self, "workspace", str(path))


@dataclass(frozen=True)
class HostStartGrant:
    request: HostStartRequest
    tenant: str
    principal: str
    session_ref: str
    policy_revision: str
    revision: str
    expires_at_ms: int
    action: Literal["host.start"]


@dataclass(frozen=True, repr=False)
class HostSessionBinding:
    """Owner attestation, never reconstructed from matching actor fields."""

    state: SessionState
    graph_session: GraphSession
    expires_at_ms: int


class HostStartGrantProvider(Protocol):
    async def authorize(
        self, state: SessionState, request: HostStartRequest
    ) -> HostStartGrant | None: ...


class HostSessionBindingProvider(Protocol):
    async def attest(
        self, state: SessionState, graph_session: GraphSession
    ) -> HostSessionBinding | None: ...


@dataclass(frozen=True, repr=False)
class AdmittedHostStart:
    """Checked composition facts; consumers must refresh before later starts."""

    request: HostStartRequest
    state: SessionState
    graph_session: GraphSession
    grant: HostStartGrant
    expires_at_ms: int


class HostStartAdmission:
    def __init__(
        self,
        sessions: SessionAuthority,
        grants: HostStartGrantProvider,
        bindings: HostSessionBindingProvider,
    ) -> None:
        self._sessions, self._grants, self._bindings = sessions, grants, bindings

    async def admit(
        self, credential: str, request: HostStartRequest, graph_session: GraphSession
    ) -> AdmittedHostStart:
        from arbora.api.contracts.session import GraphSession

        self._check_credential(credential)
        if not isinstance(graph_session, GraphSession):
            raise PermissionError("workspace_denied")
        graph_session.engine_verified_context()
        state = await self._sessions.resolve_session(credential)
        if not isinstance(state, SessionState) or state.expires_at_ms <= int(
            time.time() * 1000
        ):
            raise PermissionError("workspace_denied")
        binding = await self._attest(state, graph_session)
        grant = await self._grants.authorize(state, request)
        if await self._attest(state, graph_session) != binding:
            raise PermissionError("workspace_denied")
        refreshed = await self._sessions.resolve_session(credential)
        claims = graph_session.engine_verified_context()
        if refreshed != state or not isinstance(grant, HostStartGrant):
            raise PermissionError("workspace_denied")
        if any(
            claims.get(key) != state.resolution.request_context[key]
            for key in ("principal", "tenant", "audience", "policy_version")
        ):
            raise PermissionError("workspace_denied")
        self._check_grant(grant, state, request)
        expiry = self._expiry(state, grant, binding)
        return AdmittedHostStart(request, state, graph_session, grant, expiry)

    async def _attest(
        self, state: SessionState, graph_session: GraphSession
    ) -> HostSessionBinding:
        binding = await self._bindings.attest(state, graph_session)
        if (
            not isinstance(binding, HostSessionBinding)
            or binding.graph_session is not graph_session
            or binding.state != state
            or type(binding.expires_at_ms) is not int
            or binding.expires_at_ms <= int(time.time() * 1000)
        ):
            raise PermissionError("workspace_denied")
        return binding

    @staticmethod
    def _check_credential(credential: str) -> None:
        if (
            type(credential) is not str
            or not credential
            or len(credential) > 16384
            or any(
                character.isspace() or ord(character) < 32 for character in credential
            )
        ):
            raise PermissionError("workspace_denied")

    @staticmethod
    def _expiry(
        state: SessionState, grant: HostStartGrant, binding: HostSessionBinding
    ) -> int:
        now = int(time.time() * 1000)
        expiries = (state.expires_at_ms, grant.expires_at_ms, binding.expires_at_ms)
        if any(type(expiry) is not int or expiry <= now for expiry in expiries):
            raise PermissionError("workspace_denied")
        if max(grant.expires_at_ms, binding.expires_at_ms) > state.expires_at_ms:
            raise PermissionError("workspace_denied")
        return min(expiries)

    @staticmethod
    def _check_grant(
        grant: HostStartGrant, state: SessionState, request: HostStartRequest
    ) -> None:
        context = state.resolution.request_context
        if (
            grant.request != request
            or grant.action != "host.start"
            or grant.tenant != context["tenant"]
            or grant.principal != state.resolution.principal_id
            or grant.session_ref != state.session_ref
            or grant.policy_revision != context["policy_version"]
            or type(grant.revision) is not str
            or not grant.revision.strip()
        ):
            raise PermissionError("workspace_denied")


@dataclass(frozen=True, repr=False)
class HostStartContext:
    admission: HostStartAdmission
    request: HostStartRequest
    graph_session: GraphSession
    credential: str = field(repr=False)

    def check(
        self, *, workspace: str | None, interfaces: str, web_logs: bool
    ) -> AdmittedHostStart:
        if (
            workspace is None
            or str(Path(workspace).resolve(strict=True)) != self.request.workspace
            or interfaces != self.request.interfaces
            or web_logs != self.request.web_logs
        ):
            raise PermissionError("workspace_denied")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._check())
        raise ValueError("synchronous host startup requires an idle event loop")

    async def _check(self) -> AdmittedHostStart:
        async with asyncio.timeout(1):
            return await self.admission.admit(
                self.credential, self.request, self.graph_session
            )


def check_host_start(
    context: HostStartContext | None,
    *,
    workspace: str | None,
    terminal: bool,
    web: bool | None,
    web_logs: bool,
) -> None:
    """Validate injected admission while missing client composition still refuses."""
    if context is None:
        return
    interfaces = {
        (True, True): "both",
        (True, False): "terminal",
        (False, True): "web",
    }.get((terminal, web is True))
    if interfaces is None:
        raise ValueError("invalid_launch")
    context.check(workspace=workspace, interfaces=interfaces, web_logs=web_logs)
