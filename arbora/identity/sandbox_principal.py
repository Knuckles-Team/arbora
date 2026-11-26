"""Sandbox principals for the local no-auth profile (ARBORA-IDENTITY-R004.10).

The sandbox may mint one principal, or two for cross-tenant tests, each with
one declared role and its own tenant. A principal holds no credential: the engine
admits it from the ``sandbox`` field of the no-auth envelope its client sends, as an ordinary agent of its tenant with user-class
scopes only. The listener must be loopback and the profile must be the sandbox.
"""

from __future__ import annotations

import inspect
import ipaddress
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

SANDBOX_TENANTS: tuple[str, str] = ("sandbox-a", "sandbox-b")
ROLES: tuple[str, ...] = ("operator", "viewer")


class SandboxPrincipalRefusal(Exception):
    """A typed refusal: one code, and a message that names no credential."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class SandboxPrincipal:
    """One sandbox principal; its admission names the principal and tenant only."""

    principal: str
    tenant: str
    role: str

    @property
    def admission(self) -> dict[str, str]:
        """The ``sandbox`` object of the engine's no-auth envelope."""
        return {"principal": self.principal, "tenant": self.tenant}

    def __repr__(self) -> str:
        return (
            f"SandboxPrincipal(principal={self.principal!r}, "
            f"tenant={self.tenant!r}, role={self.role!r})"
        )


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def mint_sandbox_principals(
    roles: Sequence[tuple[str, str]],
    *,
    profile: str,
    bind_host: str,
) -> tuple[SandboxPrincipal, ...]:
    """Mint at most two principals from ``(role, tenant)`` pairs.

    Refuses outside the sandbox profile, on a non-loopback bind, and on a
    repeated role-tenant pair, tenant, or a count other than one or two.
    """
    if profile != "sandbox":
        raise SandboxPrincipalRefusal(
            "SANDBOX_PRINCIPAL_OUTSIDE_PROFILE",
            "a sandbox principal exists only in the sandbox profile",
        )
    if not _is_loopback(bind_host):
        raise SandboxPrincipalRefusal(
            "SANDBOX_PRINCIPAL_NOT_LOOPBACK",
            "the sandbox listener must be a literal loopback address",
        )
    if not 1 <= len(roles) <= 2:
        raise SandboxPrincipalRefusal(
            "SANDBOX_PRINCIPAL_DUPLICATE", "the sandbox mints one or two principals"
        )
    tenants = [tenant for _, tenant in roles]
    if len(set(tenants)) != len(tenants) or any(
        t not in SANDBOX_TENANTS for t in tenants
    ):
        raise SandboxPrincipalRefusal(
            "SANDBOX_PRINCIPAL_DUPLICATE", "each principal needs its own sandbox tenant"
        )
    minted: list[SandboxPrincipal] = []
    for role, tenant in roles:
        if role not in ROLES:
            raise SandboxPrincipalRefusal(
                "SANDBOX_PRINCIPAL_OUTSIDE_PROFILE",
                "the role is not a declared sandbox role",
            )
        minted.append(
            SandboxPrincipal(
                principal=f"sandbox-{role}-{tenant}",
                tenant=tenant,
                role=role,
            )
        )
    return tuple(minted)


def connect_kwargs(
    principal: SandboxPrincipal, connect: Callable[..., Any]
) -> dict[str, Any]:
    """The no-auth connect arguments that send this principal's sandbox envelope.

    A client without the ``sandbox`` parameter cannot send the envelope, so
    this refuses rather than connecting as the shared anonymous principal.
    """
    parameters = inspect.signature(connect).parameters
    if "sandbox" not in parameters:
        raise SandboxPrincipalRefusal(
            "UNAVAILABLE", "the installed engine client does not admit a sandbox tenant"
        )
    return {"auth_mode": "none", "sandbox": principal.admission}


__all__ = [
    "ROLES",
    "SANDBOX_TENANTS",
    "SandboxPrincipal",
    "SandboxPrincipalRefusal",
    "connect_kwargs",
    "mint_sandbox_principals",
]
