"""Server-only browser-control bindings, requests, and receipts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from arbora.api.domain.browser_control.browser_control_common import (
    DEFAULT_LEASE_SECONDS,
    DIGEST,
    ERROR_CODE,
    MAX_LEASE_SECONDS,
    TRUST_EVIDENCE,
    CancellationEffect,
    LangfuseStatus,
    bounded_json,
    checked_identifier,
    checked_opaque_ref,
    timestamps_are_finite,
    trusted_issuer,
)
from arbora.security.session import GraphSession

_Identifier = Annotated[str, AfterValidator(checked_identifier)]
_OpaqueReference = Annotated[str, AfterValidator(checked_opaque_ref)]
_Digest = Annotated[str, Field(pattern=DIGEST.pattern)]
_MachineCode = Annotated[str, Field(pattern=ERROR_CODE.pattern)]
_JsonObject = Annotated[dict[str, Any], AfterValidator(bounded_json)]


def _validate_binding_references(binding: BrowserChannelBinding) -> None:
    refs = (
        binding.login_session_ref,
        binding.browser_session_ref,
        binding.principal_ref,
        binding.document_ref,
        binding.attended_arm_ref,
    )
    for value in refs:
        checked_opaque_ref(value)
    required_prefixes = (
        (binding.login_session_ref, "login_", "login session"),
        (binding.browser_session_ref, "browser_", "browser session"),
        (binding.document_ref, "document_", "document"),
        (binding.attended_arm_ref, "attended_", "attended arm"),
    )
    for value, prefix, label in required_prefixes:
        if not value.startswith(prefix):
            raise ValueError(f"{label} reference has no verified server prefix")
    checked_identifier(binding.route_id)
    generation = binding.registration_generation
    if isinstance(generation, bool) or not 1 <= generation <= 9_007_199_254_740_991:
        raise ValueError("registration_generation must be a JS-safe positive integer")


def _validate_binding_timing(binding: BrowserChannelBinding) -> None:
    timestamps = (
        binding.attended_arm_expires_at,
        binding.access_token_expires_at,
        binding.attended_arm_issued_at,
        binding.attended_auth_time,
    )
    if not timestamps_are_finite(timestamps) or min(timestamps[:2]) <= 0:
        raise ValueError("browser binding expiries must be finite and positive")
    if binding.attended_arm_expires_at > binding.access_token_expires_at:
        raise ValueError("attended arm cannot outlive the access token")
    if not 0 < binding.attended_auth_time <= binding.attended_arm_issued_at:
        raise ValueError("attended authentication evidence is not chronological")
    lifetime = binding.attended_arm_expires_at - binding.attended_arm_issued_at
    if lifetime <= 0 or lifetime > MAX_LEASE_SECONDS:
        raise ValueError(
            "attended arm lifetime must be positive and at most fifteen minutes"
        )


def _validate_binding_evidence(binding: BrowserChannelBinding) -> None:
    for digest in (binding.catalog_digest, binding.tool_scope_digest):
        if DIGEST.fullmatch(digest) is None:
            raise ValueError("browser binding digest must be canonical sha256")
    if TRUST_EVIDENCE.fullmatch(binding.attended_acr) is None:
        raise ValueError("attended authentication evidence is invalid")
    trusted_issuer(binding.attended_issuer)
    if not callable(binding.session_revalidator):
        raise ValueError("binding session revalidator must be callable")
    if binding.attended is not True:
        raise ValueError("browser binding must be explicitly attended")


@dataclass(frozen=True, slots=True)
class BrowserChannelBinding:
    """Server-verified authority attached to one browser document channel."""

    session: GraphSession
    login_session_ref: str
    browser_session_ref: str
    principal_ref: str
    origin: str
    document_ref: str
    route_id: str
    registration_generation: int
    attended_arm_ref: str
    attended_arm_expires_at: float
    access_token_expires_at: float
    catalog_digest: str
    tool_scope_digest: str
    attended_arm_issued_at: float
    attended_auth_time: float
    attended_acr: str
    attended_issuer: str
    session_revalidator: Callable[[], Awaitable[bool]]
    attended: bool

    def __post_init__(self) -> None:
        _validate_binding_references(self)
        _validate_binding_timing(self)
        _validate_binding_evidence(self)


class IssueLeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_ref: _OpaqueReference
    tool_ids: tuple[_Identifier, ...] = Field(min_length=1, max_length=64)
    requested_ttl_seconds: int = Field(
        default=DEFAULT_LEASE_SECONDS, ge=1, le=MAX_LEASE_SECONDS, strict=True
    )
    attended: Literal[True]

    @field_validator("tool_ids")
    @classmethod
    def _tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("tool_ids must be unique")
        return value


class RenewLeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_id: _Identifier
    requested_ttl_seconds: int = Field(
        default=DEFAULT_LEASE_SECONDS,
        ge=1,
        le=DEFAULT_LEASE_SECONDS,
        strict=True,
    )
    attended: Literal[True]


class RevokeLeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_id: _Identifier
    reason: _MachineCode = Field(min_length=1, max_length=64)


class BrowserLeaseStatus(StrEnum):
    """Whether a browser control lease is live, revoked or timed out."""

    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class BrowserLeaseReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_id: str
    document_ref: str
    tool_ids: tuple[str, ...]
    registration_generation: int
    expires_at: float
    hard_expires_at: float
    status: BrowserLeaseStatus


class CatalogRegistrationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authority: Literal["arbora"] = "arbora"
    route_id: str
    registration_generation: int
    catalog_digest: str
    tool_scope_digest: str


class BrowserCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_id: _Identifier
    request_id: _Identifier
    tool_id: _Identifier
    schema_digest: _Digest
    arguments: _JsonObject
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)


class BrowserProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_ref: _OpaqueReference
    registration_generation: int = Field(ge=1, le=9_007_199_254_740_991, strict=True)
    tool_id: _Identifier
    schema_digest: _Digest
    arguments: Annotated[
        dict[str, Any], AfterValidator(lambda value: bounded_json(value, limit=8192))
    ]
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)


class CancelCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: _Identifier
    reason: _MachineCode = Field(min_length=1, max_length=64)


class ReconcileCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: _Identifier


class BrowserCallStatus(StrEnum):
    """Where one browser control call ended, or that it is still waiting."""

    PENDING_CONFIRMATION = "pending_confirmation"
    DISPATCHED = "dispatched"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    DENIED = "denied"


class BrowserCallReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    lease_id: str
    status: BrowserCallStatus
    effect: CancellationEffect
    result: Any | None = None
    result_digest: str | None = None
    error_code: str | None = None
    run_trace_id: str
    langfuse_status: LangfuseStatus


__all__ = [
    "BrowserCallReceipt",
    "BrowserCallRequest",
    "BrowserChannelBinding",
    "BrowserLeaseReceipt",
    "CatalogRegistrationReceipt",
    "CancelCallRequest",
    "IssueLeaseRequest",
    "ReconcileCallRequest",
    "RenewLeaseRequest",
    "RevokeLeaseRequest",
]


class BrowserDiscoveryRequest(BaseModel):
    """No caller-supplied owner or tenant selectors are accepted."""

    model_config = ConfigDict(extra="forbid")


class BrowserDocumentCatalog(BaseModel):
    """A live registration already admitted by the existing channel authority."""

    document_ref: _OpaqueReference
    registration_generation: int = Field(ge=1)
    tools: tuple[dict[str, Any], ...]


class BrowserDiscoveryReceipt(BaseModel):
    """Bridge readiness does not attest the browser's native API support."""

    bridge_available: bool
    browser_api: Literal["not_attested"] = "not_attested"
    documents: tuple[BrowserDocumentCatalog, ...] = ()
