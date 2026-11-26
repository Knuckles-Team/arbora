"""The identity names a deployment supplies (ARBORA-IDENTITY-R015).

The runbook in ``docs/identity.md`` tells an operator to deliver ``ARBORA_IDENTITY_ISSUER``,
``ARBORA_IDENTITY_TENANT`` and the optional ``ARBORA_SETUP_CODE`` through the secret
backend. This module is the one place that reads them. The setup code is never returned
as a value: only whether one is configured leaves this module.
"""

from __future__ import annotations

from dataclasses import dataclass

from arbora.core.settings import setting

ISSUER_ENV = "ARBORA_IDENTITY_ISSUER"
TENANT_ENV = "ARBORA_IDENTITY_TENANT"
SETUP_CODE_ENV = "ARBORA_SETUP_CODE"


@dataclass(frozen=True, slots=True)
class IdentityDeployment:
    """The issuer and tenant overrides, and whether a one-time setup code is delivered."""

    issuer: str | None
    tenant: str | None
    setup_code_configured: bool

    def report(self) -> dict[str, object]:
        """The doctor view: names present or absent, never a secret value."""
        return {
            ISSUER_ENV: self.issuer is not None,
            TENANT_ENV: self.tenant is not None,
            SETUP_CODE_ENV: self.setup_code_configured,
        }


def _text(name: str) -> str | None:
    value = str(setting(name, "") or "").strip()
    return value or None


def identity_deployment() -> IdentityDeployment:
    """Read the three names afresh; nothing is cached."""
    return IdentityDeployment(
        issuer=_text(ISSUER_ENV),
        tenant=_text(TENANT_ENV),
        setup_code_configured=_text(SETUP_CODE_ENV) is not None,
    )
