"""Scoped credential reuse (ARBORA-IDENTITY-R023.1 to R023.5).

One principal's provider credential is used by another agent only under an explicit engine
grant for that agent, connector and purpose. A missing credential starts one inline PKCE
challenge (120 s). Every collaborator is an injected port, so the module holds no state that
correctness depends on beyond the in-flight refresh lock of a grant.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import SimpleNamespace
from typing import Any, Protocol
from urllib.parse import urlsplit

from arbora.security.audit_event import audit_event

logger = logging.getLogger(__name__)

__all__ = [
    "CHALLENGE_DEADLINE_SECONDS",
    "AuthorizationChallenge",
    "CredentialRefusal",
    "CredentialRefused",
    "CredentialUse",
    "CredentialUseGrant",
    "CredentialUseRequest",
    "resolve_credential",
]

CHALLENGE_DEADLINE_SECONDS = 120
TOKEN_PROFILES = frozenset({"jwt_oidc", "opaque"})
_SUPPLIED_ACTOR_FIELDS = frozenset(
    {"actor", "principal", "owner", "connector", "agent"}
)


class CredentialRefusal(StrEnum):
    REQUEST_INVALID = "CREDENTIAL_REQUEST_INVALID"
    MISSING = "CREDENTIAL_MISSING"
    REUSE_DENIED = "REUSE_DENIED"
    BROWSER_LAUNCH_FAILED = "BROWSER_LAUNCH_FAILED"
    CALLBACK_UNAVAILABLE = "CALLBACK_UNAVAILABLE"
    INPUT_TIMEOUT = "INPUT_TIMEOUT"
    STATE_MISMATCH = "STATE_MISMATCH"
    INSECURE_REDIRECT = "INSECURE_REDIRECT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    REVOKED = "CREDENTIAL_REVOKED"
    EXPIRED = "CREDENTIAL_EXPIRED"
    INTERACTION_REQUIRED = "INTERACTION_REQUIRED"


class CredentialRefused(Exception):
    """A refusal; its text is the code alone (never a code, verifier or token)."""

    def __init__(self, refusal: CredentialRefusal) -> None:
        super().__init__(refusal.value)
        self.refusal = refusal


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise CredentialRefused(CredentialRefusal.REQUEST_INVALID)
    return value


@dataclass(frozen=True, slots=True)
class CredentialUseRequest:
    tenant: str
    owner_principal: str
    requesting_principal: str
    client: str
    connector: str
    provider: str
    audience: str
    scopes: tuple[str, ...]
    resource: str
    operation: str

    def __post_init__(self) -> None:
        for name in (
            "tenant",
            "owner_principal",
            "requesting_principal",
            "client",
            "connector",
            "provider",
            "audience",
            "resource",
            "operation",
        ):
            _text(getattr(self, name))
        scopes = tuple(sorted({_text(scope) for scope in self.scopes}))
        if not scopes:
            raise CredentialRefused(CredentialRefusal.REQUEST_INVALID)
        object.__setattr__(self, "scopes", scopes)

    @classmethod
    def from_verified(
        cls,
        caller: Any,
        params: Mapping[str, Any],
        *,
        connector: str,
        owner: str | None = None,
    ) -> CredentialUseRequest:
        """Build a request from the verified caller and the admitted connector only.

        A request field that names an actor, owner or connector grants nothing: it refuses.
        """
        if _SUPPLIED_ACTOR_FIELDS & set(params):
            raise CredentialRefused(CredentialRefusal.REQUEST_INVALID)
        return cls(
            tenant=str(caller.tenant),
            owner_principal=owner or str(caller.principal),
            requesting_principal=str(caller.principal),
            client=str(getattr(caller, "client", None) or caller.principal),
            connector=connector,
            provider=str(params.get("provider", "")),
            audience=str(params.get("audience", "")),
            scopes=tuple(params.get("scopes", ())),
            resource=str(params.get("resource", "")),
            operation=str(params.get("operation", "")),
        )


@dataclass(frozen=True, slots=True)
class CredentialUseGrant:
    request: CredentialUseRequest
    engine_decision_ref: str
    vault_ref: str
    expires_at: int
    revocation_revision: int
    token_profile: str

    def __post_init__(self) -> None:
        if self.token_profile not in TOKEN_PROFILES:
            raise CredentialRefused(CredentialRefusal.REQUEST_INVALID)
        _text(self.engine_decision_ref)
        _text(self.vault_ref)


@dataclass(frozen=True, slots=True)
class AuthorizationChallenge:
    request: CredentialUseRequest
    issuer: str
    client_id: str
    callback: str
    state: str
    nonce: str
    pkce_challenge: str
    attempt: int
    deadline: int

    def __post_init__(self) -> None:
        parts = urlsplit(self.callback)
        loopback = parts.hostname in {"localhost", "127.0.0.1", "::1"}
        if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
            raise CredentialRefused(CredentialRefusal.INSECURE_REDIRECT)
        for name in ("issuer", "client_id", "state", "nonce", "pkce_challenge"):
            _text(getattr(self, name))


class CredentialUse(Protocol):
    """The ports of one resolution; each answers from the engine, vault or provider."""

    decide: Callable[[CredentialUseRequest], Awaitable[Mapping[str, Any]]]
    vault_get: Callable[[str], Awaitable[Mapping[str, Any] | None]]
    vault_put: Callable[[str, Mapping[str, Any]], Awaitable[str]]
    validate: Callable[[Mapping[str, Any], CredentialUseGrant], Awaitable[str]]
    refresh: Callable[
        [Mapping[str, Any], CredentialUseGrant], Awaitable[Mapping[str, Any]]
    ]
    challenge: Callable[
        [CredentialUseRequest, int], Awaitable[AuthorizationChallenge | None]
    ]
    complete: Callable[[AuthorizationChallenge, float], Awaitable[Mapping[str, Any]]]
    emit: Callable[[Mapping[str, str]], None]
    clock: Callable[[], float]


_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}


async def resolve_credential(
    request: CredentialUseRequest,
    ports: CredentialUse,
    *,
    op: Any = None,
    attempt: int = 1,
) -> Mapping[str, Any]:
    """Return the usable credential record, or raise :class:`CredentialRefused`."""
    evidence: dict[str, str] = {}
    try:
        grant = await _authorize(request, ports, evidence)
        record = await ports.vault_get(grant.vault_ref)
        if record is None:
            record = await _acquire(request, grant, ports, evidence, attempt)
        standing = await ports.validate(record, grant)
        if standing == "expired":
            record = await _refresh_once(record, grant, ports, evidence)
        elif standing == "revoked":
            raise CredentialRefused(CredentialRefusal.REVOKED)
        evidence["outcome"] = "ok"
        return record
    except CredentialRefused as refused:
        evidence["refusal_code"] = refused.refusal.value
        evidence["outcome"] = "refused"
        raise
    finally:
        _audit(request, ports, evidence, op)


async def _authorize(
    request: CredentialUseRequest, ports: CredentialUse, evidence: dict[str, str]
) -> CredentialUseGrant:
    try:
        answer = await ports.decide(request)
    except Exception:  # an unanswered question grants nothing
        logger.warning("credential authority call failed", exc_info=True)
        raise CredentialRefused(CredentialRefusal.PROVIDER_UNAVAILABLE) from None
    if not answer.get("allow"):
        raise CredentialRefused(CredentialRefusal.REUSE_DENIED)
    if int(answer.get("expires_at", 0)) * 1000 <= int(ports.clock() * 1000):
        raise CredentialRefused(CredentialRefusal.EXPIRED)
    evidence["engine_decision_ref"] = str(answer.get("decision_ref", ""))
    cross = request.owner_principal != request.requesting_principal
    evidence["use"] = "cross_agent" if cross else "own"
    return CredentialUseGrant(
        request=request,
        engine_decision_ref=str(answer.get("decision_ref", "")),
        vault_ref=str(answer.get("vault_ref", "")),
        expires_at=int(answer.get("expires_at", 0)),
        revocation_revision=int(answer.get("revocation_revision", 0)),
        token_profile=str(answer.get("token_profile", "")),
    )


async def _acquire(
    request: CredentialUseRequest,
    grant: CredentialUseGrant,
    ports: CredentialUse,
    evidence: dict[str, str],
    attempt: int,
) -> Mapping[str, Any]:
    """One inline PKCE challenge; persist before ready; fresh authorization to resume."""
    challenge = await ports.challenge(request, attempt)
    if challenge is None:
        raise CredentialRefused(CredentialRefusal.INTERACTION_REQUIRED)
    evidence["challenge_ref"] = challenge.state[:8]
    deadline = challenge.deadline - ports.clock()
    if not 0 < deadline <= CHALLENGE_DEADLINE_SECONDS:
        raise CredentialRefused(CredentialRefusal.INPUT_TIMEOUT)
    try:
        answer = await asyncio.wait_for(ports.complete(challenge, deadline), deadline)
    except TimeoutError:
        raise CredentialRefused(CredentialRefusal.INPUT_TIMEOUT) from None
    if answer.get("state") != challenge.state:
        raise CredentialRefused(CredentialRefusal.STATE_MISMATCH)
    token = answer.get("token")
    if not isinstance(token, Mapping) or not token:
        raise CredentialRefused(CredentialRefusal.MISSING)
    try:
        evidence["vault_receipt"] = await ports.vault_put(grant.vault_ref, token)
    except Exception:  # nothing is ready until it is stored
        logger.warning("credential authority call failed", exc_info=True)
        raise CredentialRefused(CredentialRefusal.PERSISTENCE_FAILED) from None
    evidence["issuance"] = challenge.state[:8]
    await _authorize(request, ports, {})  # fresh engine authorization before resuming
    return token


async def _refresh_once(
    record: Mapping[str, Any],
    grant: CredentialUseGrant,
    ports: CredentialUse,
    evidence: dict[str, str],
) -> Mapping[str, Any]:
    """One refresh at a time per grant; a waiting caller reads the refreshed record."""
    lock = _REFRESH_LOCKS.setdefault(grant.engine_decision_ref, asyncio.Lock())
    async with lock:
        current = await ports.vault_get(grant.vault_ref)
        if current is not None and await ports.validate(current, grant) == "valid":
            return current
        try:
            fresh = await ports.refresh(record, grant)
            evidence["vault_receipt"] = await ports.vault_put(grant.vault_ref, fresh)
        except CredentialRefused:
            raise
        except Exception:  # the narrowed authority never widens on failure
            logger.warning("credential authority call failed", exc_info=True)
            raise CredentialRefused(CredentialRefusal.EXPIRED) from None
        evidence["refresh"] = grant.engine_decision_ref[:8]
        return fresh


def _audit(
    request: CredentialUseRequest,
    ports: CredentialUse,
    evidence: dict[str, str],
    op: Any,
) -> None:
    caller = SimpleNamespace(
        principal=request.requesting_principal, tenant=request.tenant, request_id=""
    )
    fields = {
        "client": request.client,
        "provider": request.provider,
        "scopes": " ".join(request.scopes),
        "resource": request.resource,
        "credential_operation": request.operation,
        "owner": request.owner_principal,
        **evidence,
    }
    event = audit_event(
        op
        or SimpleNamespace(
            id="credential.use", executor=SimpleNamespace(value="caller")
        ),
        {},
        caller,
        SimpleNamespace(value="fleet"),
        evidence.get("outcome", "refused"),
        evidence={key: value for key, value in fields.items() if value},
    )
    ports.emit(event)
