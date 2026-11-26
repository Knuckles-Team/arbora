"""Signed launch receipt for the terminal log-consumer handoff (ARBORA-HOST-R009.5).

A launch is an engine-recorded node: the ``host.start`` control lease. Its
lease id is the launch id. Arbora signs (launch id, host, digest) with its one
signer, so a reader can check the handoff offline and read nothing on any
failed check. The digest binds the grant's tenant, principal, session,
workspace, interfaces, log flag, configuration, policy revision, generation
and expiry.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any, Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field

from arbora.agent_host.start_admission import HostStartGrant

__all__ = [
    "LAUNCH_RECEIPT_DOMAIN",
    "HostLaunchReceipt",
    "LaunchSigner",
    "compose_host_start_issuer",
    "issue_launch_receipt",
    "launch_digest",
    "verify_launch_receipt",
]

LAUNCH_RECEIPT_DOMAIN = "arbora.host.launch-receipt"


class LaunchSigner(Protocol):
    key_id: str

    def sign(self, data: bytes) -> bytes: ...


class HostLaunchReceipt(BaseModel):
    """The handoff a terminal reader holds; it grants nothing without the checks."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    launch_id: str = Field(min_length=1, max_length=512)
    host: str = Field(min_length=1, max_length=256)
    tenant: str = Field(min_length=1, max_length=256)
    principal: str = Field(min_length=1, max_length=512)
    session_ref: str = Field(min_length=1, max_length=512)
    workspace: str = Field(min_length=1, max_length=4096)
    interfaces: Literal["terminal", "web", "both"]
    web_logs: bool
    configuration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_revision: str = Field(min_length=1, max_length=256)
    generation: str = Field(min_length=1, max_length=256)
    expires_at_ms: int = Field(gt=0)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(min_length=1, max_length=256)
    key_id: str = Field(min_length=1, max_length=128)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def launch_digest(**binding: Any) -> str:
    """SHA-256 over the canonical grant binding of one launch."""
    return hashlib.sha256(_canonical(binding)).hexdigest()


def _binding(receipt: HostLaunchReceipt | dict[str, Any]) -> dict[str, Any]:
    data = receipt if isinstance(receipt, dict) else receipt.model_dump()
    keys = (
        "tenant",
        "principal",
        "session_ref",
        "workspace",
        "interfaces",
        "web_logs",
        "configuration_digest",
        "policy_revision",
        "generation",
        "expires_at_ms",
    )
    return {key: data[key] for key in keys}


def _signed_bytes(launch_id: str, host: str, digest: str) -> bytes:
    return _canonical([LAUNCH_RECEIPT_DOMAIN, launch_id, host, digest])


def issue_launch_receipt(
    grant: HostStartGrant,
    *,
    launch_id: str,
    host: str,
    signer: LaunchSigner,
) -> HostLaunchReceipt:
    """Sign the receipt of one admitted launch with Arbora's one signer."""
    request = grant.request
    binding = {
        "tenant": grant.tenant,
        "principal": grant.principal,
        "session_ref": grant.session_ref,
        "workspace": request.workspace,
        "interfaces": request.interfaces,
        "web_logs": request.web_logs,
        "configuration_digest": request.configuration_digest,
        "policy_revision": grant.policy_revision,
        "generation": grant.revision,
        "expires_at_ms": grant.expires_at_ms,
    }
    digest = launch_digest(**binding)
    raw = signer.sign(_signed_bytes(launch_id, host, digest))
    return HostLaunchReceipt(
        launch_id=launch_id,
        host=host,
        digest=digest,
        signature=base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
        key_id=signer.key_id,
        **binding,
    )


def verify_launch_receipt(
    receipt: HostLaunchReceipt,
    *,
    public_key: Ed25519PublicKey,
    host: str,
    now_ms: int,
) -> bool:
    """Whether the receipt allows log reads; any failed check is ``False``."""
    if (
        receipt.host != host
        or not receipt.web_logs
        or receipt.interfaces not in {"terminal", "both"}
        or now_ms >= receipt.expires_at_ms
        or launch_digest(**_binding(receipt)) != receipt.digest
    ):
        return False
    padded = receipt.signature + "=" * (-len(receipt.signature) % 4)
    try:
        public_key.verify(
            base64.urlsafe_b64decode(padded),
            _signed_bytes(receipt.launch_id, receipt.host, receipt.digest),
        )
    except (InvalidSignature, binascii.Error, ValueError):
        return False
    return True


def compose_host_start_issuer(client: Any, sessions: Any, signer: LaunchSigner) -> Any:
    """The production grant issuer: the native issuer under Arbora's signer id."""
    from arbora.agent_host.host_start import NativeHostStartIssuer

    return NativeHostStartIssuer(client, sessions, issuer_ref=signer.key_id)
