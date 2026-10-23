"""Explicit broker registration with human authority and proof of possession.

This module never enrolls a key automatically or issues a human credential.
Native registration rechecks the session and applies its durable key CAS.
"""

import asyncio
import base64
import binascii
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .approval import RegisteredApprovalKey, _reference, _timestamp
from .approval_engine import approval_key, identity_value
from .ports import SessionAuthority, SessionState


@dataclass(frozen=True, slots=True, repr=False)
class ApprovalKeyRegistration:
    """Exact native registration binding; its signature is never an auth token."""

    key_id: str
    principal_id: str
    tenant: str
    purpose: Literal["signed_approval"]
    public_key_hex: str
    expected_revision: int
    expires_at_ms: int

    def __post_init__(self) -> None:
        _timestamp(self.expected_revision)
        if self.expected_revision == 2**64 - 1:
            raise ValueError("approval key revision cannot advance")
        _timestamp(self.expires_at_ms)
        for value in (self.key_id, self.principal_id, self.tenant):
            _reference(value)
        # Reuse exact key encoding and purpose validation; creation expects zero.
        RegisteredApprovalKey(
            self.key_id,
            self.tenant,
            self.principal_id,
            1,
            self.public_key_hex,
            self.expires_at_ms,
            None,
            self.purpose,
        )

    def signing_bytes(self) -> bytes:
        """Match EG registration_message's domain and length-delimited fields."""
        result = bytearray(b"eg/identity/approval-key-registration/v1\0")
        for value in (
            self.principal_id,
            self.tenant,
            self.key_id,
            self.purpose,
            self.public_key_hex,
        ):
            encoded = value.encode("utf-8")
            result.extend(len(encoded).to_bytes(4, "big"))
            result.extend(encoded)
        result.extend(self.expected_revision.to_bytes(8, "big"))
        result.extend(self.expires_at_ms.to_bytes(8, "big"))
        return bytes(result)


def verify_registration_proof(binding: ApprovalKeyRegistration, signature: str) -> None:
    """Validate only possession of this exact key and registration payload."""
    try:
        if not isinstance(signature, str) or len(signature) > 88:
            raise ValueError("invalid signature encoding")
        raw = base64.b64decode(signature, altchars=b"-_", validate=True)
        if len(raw) != 64:
            raise ValueError("invalid signature length")
        Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(binding.public_key_hex)
        ).verify(raw, binding.signing_bytes())
    except (InvalidSignature, ValueError, binascii.Error, TypeError):
        raise PermissionError("approval key registration proof refused") from None


def _require_human(
    state: SessionState, binding: ApprovalKeyRegistration, now_ms: int
) -> None:
    resolution = state.resolution
    if (
        resolution.kind != "human"
        or resolution.is_bootstrap
        or resolution.principal_id != binding.principal_id
        or resolution.request_context["tenant"] != binding.tenant
        or state.expires_at_ms <= now_ms
        or binding.expires_at_ms <= now_ms
    ):
        raise PermissionError("live original human authority required")


async def register_approval_key(
    binding: ApprovalKeyRegistration,
    signature: str,
    *,
    credential: str,
    sessions: SessionAuthority,
    broker_client: Any,
    graph: str,
) -> RegisteredApprovalKey:
    """Explicit registration; no success is reported before native CAS replies."""
    async with asyncio.timeout(1):
        state = await sessions.resolve_session(credential)
    if not isinstance(state, SessionState):
        raise PermissionError("qualified human session required")
    _require_human(state, binding, time.time_ns() // 1_000_000)
    verify_registration_proof(binding, signature)
    value = await identity_value(
        broker_client,
        graph,
        {
            "family": "credential",
            "op": "register_approval_key",
            "request": {
                "session_token": credential,
                "binding": asdict(binding),
                "attestation": "broker_verified_ed25519",
            },
        },
        "approval_key",
    )
    key = approval_key(value)
    expected = RegisteredApprovalKey(
        binding.key_id,
        binding.tenant,
        binding.principal_id,
        binding.expected_revision + 1,
        binding.public_key_hex,
        binding.expires_at_ms,
        None,
        binding.purpose,
    )
    if key != expected:
        # A mutation may already have committed. Do not claim rollback or retry.
        raise PermissionError("native registration result disagrees")
    return key
