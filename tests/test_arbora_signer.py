"""The attestation signer reuses the one arbora signing key."""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from arbora.ecosystem import ard_signing


def test_signer_signs_with_the_one_key_and_verifies() -> None:
    signer = ard_signing.ArboraSigner(allow_ephemeral=True)
    public = Ed25519PublicKey.from_public_bytes(
        ard_signing._b64d(ard_signing.public_key_b64() or "")
    )
    signature = signer.sign(b"payload")
    public.verify(signature, b"payload")
    with pytest.raises(InvalidSignature):
        public.verify(signature, b"other")
    assert signer.key_id.startswith("arbora-")


def test_unconfigured_key_refuses_a_served_signer(monkeypatch) -> None:
    monkeypatch.setattr(ard_signing, "is_configured", lambda: False)
    with pytest.raises(RuntimeError, match="not configured"):
        ard_signing.ArboraSigner()
