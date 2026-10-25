"""Conversational ACP gateway: session policy and admission (ARBORA-ACP-001)."""

from .coding_session import (
    ClientCapability,
    CodingSessionAdmission,
    CodingSessionAdmitted,
    CodingSessionRequest,
    WorkspaceGrant,
)
from .session_policy import (
    AcpRefusal,
    AcpRefusalReason,
    AcpSessionAdmission,
    AcpSessionPolicy,
    AcpSessionSnapshot,
    PrincipalContext,
)

__all__ = [
    "HostedACPAgent",
    "serve_acp_stdio",
    "AcpRefusal",
    "AcpRefusalReason",
    "AcpSessionAdmission",
    "AcpSessionPolicy",
    "AcpSessionSnapshot",
    "PrincipalContext",
    "ClientCapability",
    "CodingSessionAdmission",
    "CodingSessionAdmitted",
    "CodingSessionRequest",
    "WorkspaceGrant",
]


def __getattr__(name: str):
    """Load the optional ACP SDK only when a host explicitly requests it."""
    if name in {"HostedACPAgent", "serve_acp_stdio"}:
        from . import adapter

        return getattr(adapter, name)
    raise AttributeError(name)
