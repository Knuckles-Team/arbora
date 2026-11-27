"""Resource access request, grant and evaluation (ARBORA-IDENTITY-R019.1, R019.2).

A grant never covers a host or network by proximity: it names one resource, its version,
one protocol and one operation for one subject. The engine decides
(``CheckResourceAccess``, ``MEDULLA-IDENTITY-R704``); :func:`evaluate` is the arbora-side
re-check on the decision it received, run again for every operation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

__all__ = [
    "PROTOCOLS",
    "RECHECK_RANGE",
    "DEFAULT_RECHECK_SECONDS",
    "GrantDecision",
    "ResourceAccessRequest",
    "ResourceGrant",
    "ResourceRefusal",
    "evaluate",
    "recheck_seconds",
]

PROTOCOLS = frozenset({"web", "ssh", "rdp", "database"})
DEFAULT_RECHECK_SECONDS = 5
#: ``access.resource.recheck_seconds``, unit seconds.
RECHECK_RANGE = (1, 60)
_MAX_TEXT = 512


class ResourceRefusal(StrEnum):
    """Internal reasons; denied and unknown resources share one public code."""

    GRANT_MISSING = "RESOURCE_GRANT_MISSING"
    UNKNOWN = "RESOURCE_UNKNOWN"
    ROLE_UNTRUSTED = "ROLE_UNTRUSTED"
    POLICY_STALE = "POLICY_STALE"
    GRANT_EXPIRED = "GRANT_EXPIRED"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    AUTHORITY_UNAVAILABLE = "AUTHORITY_UNAVAILABLE"
    UNSUPPORTED_ENFORCEMENT = "UNSUPPORTED_ENFORCEMENT"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
        raise ValueError(f"RESOURCE_REQUEST_INVALID: {name}")
    return value


def _pairs(value: object) -> tuple[tuple[str, str], ...]:
    items = cast(
        "Iterable[tuple[object, object]]",
        value.items() if isinstance(value, Mapping) else value,
    )
    try:
        pairs = tuple((str(k), str(v)) for k, v in items)
    except (TypeError, ValueError):
        raise ValueError("RESOURCE_REQUEST_INVALID: constraints") from None
    return tuple(sorted(pairs))


@dataclass(frozen=True, slots=True)
class ResourceAccessRequest:
    tenant: str
    principal: str
    client: str
    resource_ref: str
    protocol: str
    operation: str
    constraints: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in ("tenant", "principal", "client", "resource_ref", "operation"):
            _text(getattr(self, name), name)
        if self.protocol not in PROTOCOLS:
            raise ValueError("RESOURCE_REQUEST_INVALID: protocol")
        object.__setattr__(self, "constraints", _pairs(self.constraints))


@dataclass(frozen=True, slots=True)
class ResourceGrant:
    """What the engine's decision grants, bound to one resource version and operation."""

    engine_decision_ref: str
    #: ``user:<principal>`` or ``role:<name>``.
    subject: str
    tenant: str
    resource_ref: str
    resource_version: str
    protocol: str
    operation: str
    scope_ceiling: tuple[str, ...]
    expires_at: int
    revocation_revision: int

    def __post_init__(self) -> None:
        for name in ("engine_decision_ref", "tenant", "resource_ref", "operation"):
            _text(getattr(self, name), name)
        if (
            not self.subject.startswith(("user:", "role:"))
            or self.protocol not in PROTOCOLS
        ):
            raise ValueError("RESOURCE_REQUEST_INVALID: subject")
        object.__setattr__(self, "scope_ceiling", tuple(self.scope_ceiling))


@dataclass(frozen=True, slots=True)
class GrantDecision:
    allowed: bool
    refusal: ResourceRefusal | None = None


def evaluate(
    request: ResourceAccessRequest,
    grant: ResourceGrant | None,
    now_ms: int,
    revocation_revision: int,
    *,
    resource_version: str = "",
    trusted_roles: Iterable[str] = (),
) -> GrantDecision:
    """Allow only when every dimension matches, the grant is live and not revoked."""
    if grant is None:
        return GrantDecision(False, ResourceRefusal.GRANT_MISSING)
    if grant.tenant != request.tenant:
        return GrantDecision(False, ResourceRefusal.TENANT_MISMATCH)
    if grant.resource_ref != request.resource_ref or grant.resource_version != str(
        resource_version
    ):
        return GrantDecision(False, ResourceRefusal.UNKNOWN)
    if grant.subject.startswith("role:"):
        if grant.subject.removeprefix("role:") not in set(trusted_roles):
            return GrantDecision(False, ResourceRefusal.ROLE_UNTRUSTED)
    elif grant.subject != f"user:{request.principal}":
        return GrantDecision(False, ResourceRefusal.GRANT_MISSING)
    if grant.protocol != request.protocol or grant.operation != request.operation:
        return GrantDecision(False, ResourceRefusal.GRANT_MISSING)
    if grant.expires_at <= now_ms:
        return GrantDecision(False, ResourceRefusal.GRANT_EXPIRED)
    if grant.revocation_revision != revocation_revision:
        return GrantDecision(False, ResourceRefusal.POLICY_STALE)
    return GrantDecision(True)


def recheck_seconds(value: object = None) -> int:
    """``access.resource.recheck_seconds``: default 5, range [1, 60]."""
    if value is None:
        return DEFAULT_RECHECK_SECONDS
    low, high = RECHECK_RANGE
    if type(value) is not int or not low <= value <= high:
        raise ValueError(
            f"access.resource.recheck_seconds must be within [{low}, {high}]"
        )
    return value
