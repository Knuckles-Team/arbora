"""Cryptographic and generated-client refusal evidence, not native acceptance."""

import base64
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from arbora.identity.approval_engine import EngineOriginalSessionResolver
from arbora.identity.approval_registration import (
    ApprovalKeyRegistration,
    verify_registration_proof,
)
from arbora.identity.engine import IdentityUnavailable

pytestmark = pytest.mark.spec("ARBORA-A2A-R005.2.4")


@pytest.fixture
def signed():
    private = Ed25519PrivateKey.generate()
    binding = ApprovalKeyRegistration(
        "key:one",
        "human:one",
        "tenant:one",
        "signed_approval",
        private.public_key().public_bytes_raw().hex(),
        0,
        4000,
    )
    signature = base64.urlsafe_b64encode(private.sign(binding.signing_bytes())).decode()
    return binding, signature


def test_real_registration_signature(signed):
    binding, signature = signed
    verify_registration_proof(binding, signature)
    prefix = b"eg/identity/approval-key-registration/v1\0"
    expected = prefix
    for field in (
        binding.principal_id,
        binding.tenant,
        binding.key_id,
        binding.purpose,
        binding.public_key_hex,
    ):
        raw = field.encode()
        expected += len(raw).to_bytes(4, "big") + raw
    expected += (0).to_bytes(8, "big") + (4000).to_bytes(8, "big")
    assert binding.signing_bytes() == expected


@pytest.mark.parametrize(
    "change",
    [
        {"key_id": "key:two"},
        {"principal_id": "human:two"},
        {"tenant": "tenant:two"},
        {"expected_revision": 1},
        {"expires_at_ms": 4001},
        {"public_key_hex": "ab" * 32},
    ],
)
def test_signature_binds_every_registration_field(signed, change):
    binding, signature = signed
    with pytest.raises(PermissionError):
        verify_registration_proof(replace(binding, **change), signature)


@pytest.mark.parametrize("signature", ["", "!", "a" * 89, "AAAA"])
def test_malformed_signature_refused(signed, signature):
    with pytest.raises(PermissionError):
        verify_registration_proof(signed[0], signature)


def test_overflow_revision_refused_before_registration(signed):
    with pytest.raises(ValueError):
        replace(signed[0], expected_revision=2**64 - 1)


@pytest.mark.asyncio
async def test_published_contract_refuses_unknown_reference_before_transport():
    class Transport:
        async def _send(self, *args, **kwargs):
            pytest.fail("unsupported operation reached transport")

    resolver = EngineOriginalSessionResolver(
        Transport(), graph="identity", tenant="tenant:one"
    )
    with pytest.raises(IdentityUnavailable):
        await resolver.resolve_original_session("session:original")


@pytest.mark.asyncio
async def test_published_contract_refuses_non_touch_before_transport():
    from arbora.identity.engine_ports import EngineSessionAuthority

    class Transport:
        async def _send(self, *args, **kwargs):
            pytest.fail("unsupported non-touch request reached transport")

    authority = EngineSessionAuthority(Transport(), graph="identity", refresh=False)
    with pytest.raises(IdentityUnavailable):
        await authority.resolve_session("synthetic-credential")


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh", [True, False])
async def test_explicit_non_touch_preserves_default_request(refresh, monkeypatch):
    from epistemic_graph.generated import security

    from arbora.identity.engine_ports import EngineSessionAuthority

    class Transport:
        async def _send(self, *args, **kwargs):
            pytest.fail("test request must stop at supplied validator boundary")

    seen = []

    async def send(client, params, graph):
        seen.append(params["op"]["request"])
        raise PermissionError("supplied authority refusal")

    monkeypatch.setattr(security, "send_identity", send)
    authority = EngineSessionAuthority(Transport(), graph="identity", refresh=refresh)
    with pytest.raises(PermissionError):
        await authority.resolve_session("synthetic-credential")
    expected = {"session_token": "synthetic-credential"}
    if not refresh:
        expected["refresh"] = False
    assert seen == [expected]
