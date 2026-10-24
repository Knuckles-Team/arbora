"""Injected startup policy and exact owner-attested session binding."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from arbora.agent_host.start_configuration import (
    HostRuntimeResources,
    HostStartConfiguration,
)
from arbora.identity.ports import SessionAuthority, SessionState
from arbora.security.anonymous import session_context

if TYPE_CHECKING:
    from arbora.api.contracts.session import GraphSession


def _same_session(initial: SessionState, refreshed: SessionState) -> bool:
    """Permit idle refresh without widening the admitted lifetime or identity."""
    return (
        isinstance(refreshed, SessionState)
        and refreshed.resolution == initial.resolution
        and refreshed.session_ref == initial.session_ref
        and refreshed.mfa_at_ms == initial.mfa_at_ms
        and refreshed.expires_at_ms >= initial.expires_at_ms
    )


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
    runtime: HostRuntimeResources | None = None
    configuration_digest: str | None = None
    configuration_revision: str | None = None

    def same_binding(self, other: HostSessionBinding) -> bool:
        return (
            _same_session(self.state, other.state)
            and self.graph_session is other.graph_session
            and self.expires_at_ms == other.expires_at_ms
            and self.configuration_digest == other.configuration_digest
            and self.configuration_revision == other.configuration_revision
            and (
                self.runtime is other.runtime
                or (
                    self.runtime is not None
                    and self.runtime.same_resources(other.runtime)
                )
            )
        )


class HostStartGrantProvider(Protocol):
    """Trusted access-owner projection, not a decoder for caller lease maps.

    Each call must freshly resolve the exact tenant-scoped grant and verify
    issuer authority for host.start, the immutable request/session binding,
    current policy and lease revision, status and expiry. Kind/issuer strings
    in a ControlLease grant body do not prove issuance authority. Generic
    lease:write or graph access does not imply host-start authority.

    A lease-backed implementation must preserve lease identity together with
    its revision in the returned opaque revision token; equal revisions on
    different leases must not establish continuity. Return None when the
    owner cannot qualify any of these facts. No production owner is selected
    by this protocol, and no approval or lease policy is introduced here.
    """

    async def authorize(
        self, state: SessionState, request: HostStartRequest
    ) -> HostStartGrant | None: ...


class HostSessionBindingProvider(Protocol):
    async def attest(
        self,
        state: SessionState,
        graph_session: GraphSession,
        *,
        runtime: HostRuntimeResources | None = None,
        configuration_digest: str | None = None,
    ) -> HostSessionBinding | None:
        """Attest current owned resources and revision; absent runtime facts deny."""
        ...


@dataclass(frozen=True, repr=False)
class AdmittedHostStart:
    """Checked composition facts; consumers must refresh before later starts."""

    request: HostStartRequest
    state: SessionState
    graph_session: GraphSession
    grant: HostStartGrant
    expires_at_ms: int
    runtime: HostRuntimeResources | None = None
    configuration_revision: str | None = None


class HostStartAdmission:
    def __init__(
        self,
        sessions: SessionAuthority,
        grants: HostStartGrantProvider,
        bindings: HostSessionBindingProvider,
    ) -> None:
        self._sessions, self._grants, self._bindings = sessions, grants, bindings

    @classmethod
    def from_native_grant(
        cls,
        sessions: SessionAuthority,
        bindings: HostSessionBindingProvider,
        client: Any,
        *,
        lease_id: str,
        issuer_ref: str,
    ) -> HostStartAdmission:
        """Compose a borrowed native reader with the existing admission checks.

        The access owner supplies the authenticated client and trusted issuer.
        This does not issue a grant, change fleet policy, or select a default
        session/construction authority when either owner is unavailable.
        """
        from arbora.access.host_start import NativeHostStartGrants

        return cls(
            sessions,
            NativeHostStartGrants(client, lease_id=lease_id, issuer_ref=issuer_ref),
            bindings,
        )

    @classmethod
    async def issue_native_grant(
        cls,
        sessions: SessionAuthority,
        bindings: HostSessionBindingProvider,
        client: Any,
        credential: str,
        request: HostStartRequest,
        *,
        lease_id: str,
        issuer_ref: str,
        expires_at_ms: int,
        idempotency_key: str,
    ) -> HostStartAdmission:
        """Access-owner entrypoint; native issuer privilege is still mandatory.

        Call only after the existing owner policy authorizes this exact start.
        This does not turn a connector result or caller-controlled lease into a
        grant, and never retries an uncertain issue. Returned admission retains
        all construction/session checks at the actual launch boundary.
        """
        from arbora.access.host_start import NativeHostStartIssuer

        await NativeHostStartIssuer(client, sessions, issuer_ref=issuer_ref).issue(
            credential,
            request,
            lease_id=lease_id,
            expires_at_ms=expires_at_ms,
            idempotency_key=idempotency_key,
        )
        return cls.from_native_grant(
            sessions, bindings, client, lease_id=lease_id, issuer_ref=issuer_ref
        )

    async def admit(
        self,
        credential: str,
        request: HostStartRequest,
        graph_session: GraphSession,
        *,
        configuration: HostStartConfiguration | None = None,
        runtime: HostRuntimeResources | None = None,
    ) -> AdmittedHostStart:
        from arbora.api.contracts.session import GraphSession

        self._check_configuration(request, configuration, runtime)
        self._check_credential(credential)
        if not isinstance(graph_session, GraphSession):
            raise PermissionError("workspace_denied")
        session_context(graph_session)
        state = await self._sessions.resolve_session(credential)
        self._check_configuration(request, configuration, runtime)
        if not isinstance(state, SessionState) or state.expires_at_ms <= int(
            time.time() * 1000
        ):
            raise PermissionError("workspace_denied")
        binding = await self._attest(
            state, graph_session, request, configuration, runtime
        )
        grant = await self._grants.authorize(state, request)
        self._check_configuration(request, configuration, runtime)
        repeated = await self._attest(
            state, graph_session, request, configuration, runtime
        )
        if not binding.same_binding(repeated):
            raise PermissionError("workspace_denied")
        refreshed = await self._sessions.resolve_session(credential)
        self._check_configuration(request, configuration, runtime)
        if not _same_session(state, refreshed) or not isinstance(grant, HostStartGrant):
            raise PermissionError("workspace_denied")
        final_binding = await self._attest(
            state, graph_session, request, configuration, runtime
        )
        if not binding.same_binding(final_binding):
            raise PermissionError("workspace_denied")
        claims = session_context(graph_session)
        if any(
            claims.get(key) != state.resolution.request_context[key]
            for key in ("principal", "tenant", "audience", "policy_version")
        ):
            raise PermissionError("workspace_denied")
        self._check_grant(grant, state, request)
        expiry = self._expiry(state, grant, binding)
        return AdmittedHostStart(
            request,
            state,
            graph_session,
            grant,
            expiry,
            runtime,
            binding.configuration_revision,
        )

    async def _attest(
        self,
        state: SessionState,
        graph_session: GraphSession,
        request: HostStartRequest,
        configuration: HostStartConfiguration | None,
        runtime: HostRuntimeResources | None,
    ) -> HostSessionBinding:
        if runtime is None:
            binding = await self._bindings.attest(state, graph_session)
        else:
            binding = await self._bindings.attest(
                state,
                graph_session,
                runtime=runtime,
                configuration_digest=request.configuration_digest,
            )
        self._check_configuration(request, configuration, runtime)
        if (
            not isinstance(binding, HostSessionBinding)
            or binding.graph_session is not graph_session
            or not _same_session(state, binding.state)
            or type(binding.expires_at_ms) is not int
            or binding.expires_at_ms <= int(time.time() * 1000)
        ):
            raise PermissionError("workspace_denied")
        self._check_runtime_binding(binding, runtime, request)
        return binding

    @staticmethod
    def _check_configuration(
        request: HostStartRequest,
        configuration: HostStartConfiguration | None,
        runtime: HostRuntimeResources | None,
    ) -> None:
        if configuration is None and runtime is None:
            return
        if configuration is None or runtime is None:
            raise PermissionError("configuration_unavailable")
        if configuration.digest() != request.configuration_digest:
            raise PermissionError("configuration_mismatch")
        interfaces = {
            (True, True): "both",
            (True, False): "terminal",
            (False, True): "web",
        }.get((configuration.enable_terminal_ui, configuration.enable_web_ui is True))
        if (
            configuration.workspace is None
            or str(Path(configuration.workspace).resolve(strict=True))
            != request.workspace
            or interfaces != request.interfaces
            or configuration.enable_web_logs != request.web_logs
        ):
            raise PermissionError("configuration_mismatch")

    @staticmethod
    def _check_runtime_binding(
        binding: HostSessionBinding,
        runtime: HostRuntimeResources | None,
        request: HostStartRequest,
    ) -> None:
        if runtime is None:
            return
        if (
            not runtime.same_resources(binding.runtime)
            or binding.configuration_digest != request.configuration_digest
            or type(binding.configuration_revision) is not str
            or not binding.configuration_revision.strip()
            or len(binding.configuration_revision) > 256
        ):
            raise PermissionError("configuration_unavailable")

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

    def check_runtime(
        self,
        configuration: HostStartConfiguration,
        runtime: HostRuntimeResources,
    ) -> AdmittedHostStart:
        """Return exact resources admitted by the current construction owner."""
        HostStartAdmission._check_configuration(self.request, configuration, runtime)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self._check(configuration=configuration, runtime=runtime)
            )
        raise ValueError("synchronous host startup requires an idle event loop")

    async def revalidate_runtime(
        self,
        previous: AdmittedHostStart,
        configuration: HostStartConfiguration,
        runtime: HostRuntimeResources,
    ) -> AdmittedHostStart:
        """Refresh existing owners without extending a checked start's authority."""
        checked = await self._check(configuration=configuration, runtime=runtime)
        if (
            previous.expires_at_ms <= int(time.time() * 1000)
            or checked.expires_at_ms < previous.expires_at_ms
            or checked.request != previous.request
            or checked.graph_session is not previous.graph_session
            or not _same_session(previous.state, checked.state)
            or checked.grant != previous.grant
            or checked.configuration_revision != previous.configuration_revision
            or not runtime.same_resources(previous.runtime)
        ):
            raise PermissionError("workspace_denied")
        return previous

    async def _check(
        self,
        *,
        configuration: HostStartConfiguration | None = None,
        runtime: HostRuntimeResources | None = None,
    ) -> AdmittedHostStart:
        async with asyncio.timeout(1):
            return await self.admission.admit(
                self.credential,
                self.request,
                self.graph_session,
                configuration=configuration,
                runtime=runtime,
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
