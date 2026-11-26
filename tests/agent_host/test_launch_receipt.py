"""The signed launch receipt (ARBORA-HOST-R009.5), checked without a terminal."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from arbora.agent_host.launch_receipt import (
    compose_host_start_issuer,
    issue_launch_receipt,
    verify_launch_receipt,
)
from arbora.agent_host.start_admission import HostStartGrant, HostStartRequest
from arbora.ecosystem import ard_signing

NOW = 1_000


def _grant(tmp_path, *, interfaces="terminal", web_logs=True, expires=5_000):
    request = HostStartRequest(
        workspace=str(tmp_path),
        interfaces=interfaces,
        web_logs=web_logs,
        configuration_digest="c" * 64,
    )
    return HostStartGrant(
        request=request,
        tenant="tenant-a",
        principal="user:alice",
        session_ref="session-1",
        policy_revision="p1",
        revision='["lease-1",3]',
        expires_at_ms=expires,
        action="host.start",
    )


def _public() -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(
        ard_signing._b64d(ard_signing.public_key_b64() or "")
    )


@pytest.fixture
def signer():
    return ard_signing.ArboraSigner(allow_ephemeral=True)


def test_receipt_is_signed_by_the_one_signer_and_allows_reads(tmp_path, signer):
    receipt = issue_launch_receipt(
        _grant(tmp_path), launch_id="lease-1", host="host-a", signer=signer
    )
    assert receipt.key_id == signer.key_id
    assert verify_launch_receipt(
        receipt, public_key=_public(), host="host-a", now_ms=NOW
    )


@pytest.mark.parametrize(
    "change",
    [
        {"host": "host-b"},
        {"now_ms": 5_000},
    ],
)
def test_wrong_host_or_expired_receipt_allows_zero_reads(tmp_path, signer, change):
    receipt = issue_launch_receipt(
        _grant(tmp_path), launch_id="lease-1", host="host-a", signer=signer
    )
    args = {"host": "host-a", "now_ms": NOW, **change}
    assert not verify_launch_receipt(receipt, public_key=_public(), **args)


@pytest.mark.parametrize(
    "grant",
    [{"web_logs": False}, {"interfaces": "web"}],
)
def test_disabled_or_non_terminal_handoff_allows_zero_reads(tmp_path, signer, grant):
    receipt = issue_launch_receipt(
        _grant(tmp_path, **grant), launch_id="lease-1", host="host-a", signer=signer
    )
    assert not verify_launch_receipt(
        receipt, public_key=_public(), host="host-a", now_ms=NOW
    )


@pytest.mark.parametrize(
    "tamper",
    [
        {"launch_id": "lease-2"},
        {"principal": "user:eve"},
        {"workspace": "/elsewhere"},
        {"generation": '["lease-1",4]'},
    ],
)
def test_tampered_receipt_allows_zero_reads(tmp_path, signer, tamper):
    receipt = issue_launch_receipt(
        _grant(tmp_path), launch_id="lease-1", host="host-a", signer=signer
    )
    forged = receipt.model_copy(update=tamper)
    assert not verify_launch_receipt(
        forged, public_key=_public(), host="host-a", now_ms=NOW
    )


def test_issuer_composition_uses_the_signer_key_id(signer):
    issuer = compose_host_start_issuer(object(), object(), signer)
    assert issuer._issuer_ref == signer.key_id
