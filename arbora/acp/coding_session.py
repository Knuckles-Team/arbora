"""Coding admission policy; permission exchange and transport remain unavailable."""

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum

from .session_policy import (
    AcpRefusal,
    AcpRefusalReason,
    AcpSessionAdmission,
    AcpSessionPolicy,
    AcpSessionSnapshot,
    PrincipalContext,
)


class ClientCapability(StrEnum):
    """Closed ACP capability vocabulary admitted by Arbora."""

    READ_TEXT_FILE = "fs/read_text_file"
    WRITE_TEXT_FILE = "fs/write_text_file"
    TERMINAL_CREATE = "terminal/create"
    TERMINAL_OUTPUT = "terminal/output"
    TERMINAL_WAIT_FOR_EXIT = "terminal/wait_for_exit"
    TERMINAL_KILL = "terminal/kill"
    REQUEST_PERMISSION = "session/request_permission"


def _validate_root(root: str) -> None:
    if not isinstance(root, str) or not root.strip():
        raise ValueError("workspace root must be a non-empty string")


@dataclass(frozen=True)
class CodingSessionRequest:
    """Client-declared root and capabilities; neither is a grant."""

    workspace_root: str
    client_capabilities: frozenset[ClientCapability]

    def __post_init__(self) -> None:
        _validate_root(self.workspace_root)
        object.__setattr__(
            self,
            "client_capabilities",
            frozenset(ClientCapability(item) for item in self.client_capabilities),
        )


@dataclass(frozen=True)
class WorkspaceGrant:
    """Future permission record; constructing a record confers no authority."""

    root: str
    capability: ClientCapability
    standing: bool

    def __post_init__(self) -> None:
        _validate_root(self.root)
        object.__setattr__(self, "capability", ClientCapability(self.capability))
        if not isinstance(self.standing, bool):
            raise ValueError("standing must be a boolean")


@dataclass(frozen=True)
class CodingSessionAdmitted:
    """Immutable admission snapshot, with no permission issuer attached yet."""

    snapshot: AcpSessionSnapshot
    request: CodingSessionRequest
    grants: tuple[WorkspaceGrant, ...] = field(default=(), init=False)


class CodingSessionAdmission:
    """Extend session policy without performing or forwarding host effects."""

    def __init__(
        self, policy: AcpSessionPolicy, *, trusted_roots: frozenset[str]
    ) -> None:
        self._sessions = AcpSessionAdmission(policy)
        self._trusted_roots = frozenset(trusted_roots)
        for root in self._trusted_roots:
            _validate_root(root)

    def admit(
        self,
        principal: PrincipalContext | None,
        request: CodingSessionRequest,
        *,
        chat_adapter_available: bool,
        now: datetime | None = None,
    ) -> CodingSessionAdmitted:
        snapshot = self._sessions.admit(
            principal, chat_adapter_available=chat_adapter_available, now=now
        )
        if request.workspace_root not in self._trusted_roots:
            raise AcpRefusal(AcpRefusalReason.UNTRUSTED_ROOT)
        return CodingSessionAdmitted(snapshot, request)

    def check_capability(
        self,
        session: CodingSessionAdmitted,
        method: str,
        *,
        client_connected: bool,
        chat_adapter_available: bool,
        now: datetime | None = None,
    ) -> CodingSessionAdmitted:
        """Check a declared read; no caller-supplied grant can enable effects."""
        session = self.check_preview(
            session,
            method,
            client_connected=client_connected,
            chat_adapter_available=chat_adapter_available,
            now=now,
        )
        if method != ClientCapability.READ_TEXT_FILE:
            raise AcpRefusal(AcpRefusalReason.PERMISSION_REQUIRED)
        return session

    def check_preview(
        self,
        session: CodingSessionAdmitted,
        method: str,
        *,
        client_connected: bool,
        chat_adapter_available: bool,
        now: datetime | None = None,
    ) -> CodingSessionAdmitted:
        """Validate a preview only; this never permits an effect or issues a grant."""
        if not chat_adapter_available:
            raise AcpRefusal(AcpRefusalReason.CHAT_ADAPTER_UNAVAILABLE)
        session = self.check_session(session, now=now)
        if not client_connected:
            raise AcpRefusal(AcpRefusalReason.CLIENT_DISCONNECTED)
        if method not in session.request.client_capabilities:
            raise AcpRefusal(AcpRefusalReason.CAPABILITY_NOT_DECLARED)
        return session

    def check_session(
        self, session: CodingSessionAdmitted, *, now: datetime | None = None
    ) -> CodingSessionAdmitted:
        """Validate lifetime and trusted root, without requiring a capability."""
        snapshot = self._sessions.check_message(
            session.snapshot, chat_adapter_available=True, now=now
        )
        if session.request.workspace_root not in self._trusted_roots:
            raise AcpRefusal(AcpRefusalReason.UNTRUSTED_ROOT)
        return replace(session, snapshot=snapshot)
