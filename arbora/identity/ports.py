"""Injected owner ports; these are not generated EG wire response schemas.

The public EG resolution currently lacks source expiry and browser session
facts. Composition must supply them from qualified authority or remain
unavailable. No port is selected from request headers or ASGI state.
"""

import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from .engine import IdentityUnavailable, Resolution


def native_signin_scopes() -> list[str]:
    """The sign-in scopes of the native MCP client (ARBORA-IDENTITY-R030.4.8).

    Served packages read the one declared client contract through this port,
    so the scopes that the protected-resource metadata advertises are never a
    second copy of the contract.
    """
    from .client_contract import NATIVE_DEFAULT_SCOPES

    return list(NATIVE_DEFAULT_SCOPES)


@dataclass(frozen=True, slots=True, repr=False)
class CredentialState:
    """Exact credential's current owner response, supplied by an injected port."""

    resolution: Resolution
    expires_at_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.resolution, Resolution):
            raise IdentityUnavailable("qualified credential resolution required")
        if type(self.expires_at_ms) is not int or self.expires_at_ms < 0:
            raise IdentityUnavailable("authoritative credential expiry required")


@dataclass(frozen=True, slots=True, repr=False)
class SessionState(CredentialState):
    """Backend composition facts, never a parallel BrowserSessionEvidence DTO.

    The session reference must be the actual authority binding reference, not
    a SessionView display handle. This class cannot qualify its provenance.
    All fields are explicit, including nullable MFA; there are no defaults.
    """

    session_ref: str
    mfa_at_ms: int | None

    def __post_init__(self) -> None:
        CredentialState.__post_init__(self)
        if (
            type(self.session_ref) is not str
            or not self.session_ref
            or self.session_ref != self.session_ref.strip()
            or re.fullmatch(r"[0-9a-fA-F]{12}", self.session_ref)
        ):
            raise IdentityUnavailable(
                "authoritative session binding reference required"
            )
        if self.mfa_at_ms is not None and (
            type(self.mfa_at_ms) is not int or self.mfa_at_ms < 0
        ):
            raise IdentityUnavailable("authoritative session MFA timestamp required")


class CredentialAuthority(Protocol):
    async def resolve_credential(self, credential: str) -> CredentialState:
        """Verify the exact credential and return current policy and source expiry."""
        ...


class SessionAuthority(Protocol):
    async def resolve_session(self, credential: str) -> SessionState:
        """Verify current session including revocation, rotation and expiry."""
        ...


class SigningKeyStore(Protocol):
    def get(self, name: str) -> str | None:
        """Read an existing secret-backed issuer key ring; never create one."""
        ...


class VerifiedToken(Protocol):
    """Verified claim snapshot; live revalidation is an optional owner feature."""

    @property
    def claims(self) -> Mapping[str, Any]: ...


VerifyToken = Callable[[str], Awaitable[VerifiedToken]]


class BrowserAdmissionPort(Protocol):
    """One browser request's admission, as served code may hold it.

    The composition root qualifies the concrete admission before it hands this
    port to a served package; served code never checks or builds the concrete
    type itself.
    """

    def request(
        self, scope: Mapping[str, Any]
    ) -> AbstractAsyncContextManager[Mapping[str, Any]]:
        """Admit ``scope`` and yield the forwarded scope for its whole lifetime."""
        ...


#: Read the browser session cookie from an ASGI scope: ``None`` when absent,
#: :class:`PermissionError` when a presented cookie is invalid or ambiguous.
SessionCookieReader = Callable[[Mapping[str, Any]], str | None]


class BrowserBoundaryFactory(Protocol):
    """Trusted process-root constructor, never selected by request input."""

    def __call__(
        self,
        invocation: Any,
        client_factory: Callable[[], Awaitable[Any]],
        *,
        graph: str,
        tenant: str,
        broker_claims: Callable[[], Mapping[str, Any]],
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class IdentityLifecycle:
    """Process-lifetime identity work that the composition root injects.

    ``start`` runs after the process authority is minted and ``stop`` at
    shutdown. The served process uses it to reconcile arbora's own OAuth
    client contract (ARBORA-IDENTITY-R030.4) without importing the concrete
    identity runtime.
    """

    start: Callable[[], object]
    stop: Callable[[], None]


class IdentityAdminPort(Protocol):
    """Identity administration over the engine (ARBORA-IDENTITY-R018.1).

    ``identity`` sends one engine ``Identity`` request over the caller-bound
    ``client`` and returns the value of the ``expect`` reply kind. An engine
    refusal raises an exception that carries its wire ``code``.
    """

    async def identity(
        self,
        client: Any,
        claims: Mapping[str, Any],
        family: str,
        op: str,
        request: Mapping[str, Any] | None = None,
        *,
        expect: str,
    ) -> Any: ...

    async def audit_verify(self, client: Any, claims: Mapping[str, Any]) -> Any:
        """Return the engine audit chain verdict."""
        ...


def engine_identity_admin(graph: str) -> IdentityAdminPort:
    """The engine-backed administration port, for the served composition.

    Served packages read the one port through here and never import the
    concrete class (ARBORA-IDENTITY-R018.1).
    """
    from .engine_admin import EngineIdentityAdmin

    return EngineIdentityAdmin(graph=graph)
