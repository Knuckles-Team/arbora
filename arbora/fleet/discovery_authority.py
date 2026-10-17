"""Transport-neutral authority values for private fleet discovery snapshots.

Arbora owns this value: its OAuth broker mints it and its multiplexer reads it.

The remote OAuth broker mints these non-secret values after resolving a live
grant. Lower catalog writers consume the exact type without importing the MCP
adapter that minted it. Provenance remains enforced by the multiplexer's
identity-bound side channel; this module is a value contract, not a registry or
an authority factory.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "DISCOVERY_AUTHORITY_OAUTH_GRANT",
    "DISCOVERY_AUTHORITY_TENANT_LOCAL",
    "OAuthGrantBinding",
    "TenantLocalDiscoveryBinding",
]

DISCOVERY_AUTHORITY_OAUTH_GRANT: Literal["oauth_grant"] = "oauth_grant"
DISCOVERY_AUTHORITY_TENANT_LOCAL: Literal["tenant_local"] = "tenant_local"


@dataclass(frozen=True)
class OAuthGrantBinding:
    """Non-secret identity of one broker-resolved OAuth grant."""

    tenant_id: str
    principal_id: str
    provider_id: str
    resource_url: str
    audience: str
    granted_scopes: tuple[str, ...]
    key_version: int
    grant_revision: str

    @property
    def fingerprint(self) -> str:
        material = {
            "schema": "au.oauth-grant-binding.v1",
            "tenant": self.tenant_id,
            "principal": self.principal_id,
            "provider": self.provider_id,
            "resource": self.resource_url,
            "audience": self.audience,
            "scopes": list(self.granted_scopes),
            "key_version": self.key_version,
            "grant_revision": self.grant_revision,
        }
        encoded = json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TenantLocalDiscoveryBinding:
    """Process-owned authority for a non-OAuth MCP discovery snapshot.

    Local and stdio children have no provider OAuth grant to fingerprint. The
    multiplexer creates this binding only after a verified graph session is
    present. The fixed authority label is a visibility contract, not a digest
    of roles or scopes, and never comes from catalog data.
    """

    tenant_id: str
    authority: Literal["tenant_local"] = DISCOVERY_AUTHORITY_TENANT_LOCAL

    def __post_init__(self) -> None:
        tenant = str(self.tenant_id or "").strip()
        if not tenant or self.authority != DISCOVERY_AUTHORITY_TENANT_LOCAL:
            raise ValueError("tenant-local discovery authority is malformed")
