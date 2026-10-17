"""Real detached cryptography over supplied snapshots; not served grant acceptance."""

from __future__ import annotations

import base64
from dataclasses import replace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import ValidationError

from arbora.a2a.approval import (
    ApprovalVerificationEvidence,
    PendingCallBinding,
    SignatureCheck,
    verify_pending_signature,
)
from arbora.a2a.models import A2APlanConfirmParams
from arbora.api.invoke.plan import PlanBinding, params_digest
from tests.a2a.test_application import _app
from tests.api._support import make_verified_caller

pytestmark = pytest.mark.spec("GRAPHOS-A2A-R005.2.2")
caller = make_verified_caller("kg:write")


@pytest.fixture
def signed() -> tuple[A2APlanConfirmParams, ApprovalVerificationEvidence]:
    human = caller()
    binding = PendingCallBinding(
        task_id="task:one",
        parent_work_item_id="work:parent",
        child_work_item_id="work:child",
        pending_call_id="call:one",
        plan_ref="plan:one",
        plan=PlanBinding(
            "tool.write",
            params_digest({"value": 1}),
            human.principal,
            human.tenant,
            human.policy_revision,
            params_digest({"registry": 1}),
            "write",
            "plan",
        ),
        approving_human=human.principal,
        grant_id="grant:one",
        grant_revision=1,
        issued_at_ms=1_000,
        expires_at_ms=2_000,
    )
    private_key = Ed25519PrivateKey.generate()
    confirmation = A2APlanConfirmParams(
        task_id=binding.task_id,
        pending_call_id=binding.pending_call_id,
        plan_ref=binding.plan_ref,
        signature=base64.urlsafe_b64encode(
            private_key.sign(binding.signing_bytes())
        ).decode(),
    )
    evidence = ApprovalVerificationEvidence(
        binding, human, human.principal, private_key.public_key(), "active"
    )
    return confirmation, evidence


def test_valid_signature_is_evidence_only(signed: Any) -> None:
    confirmation, evidence = signed
    assert (
        verify_pending_signature(confirmation, evidence=evidence, now_ms=1_500)
        == SignatureCheck.VALID_SIGNATURE_ONLY
    )
    # This function is deliberately stateless: replay prevention belongs to EG CAS.
    assert (
        verify_pending_signature(confirmation, evidence=evidence, now_ms=1_500)
        == SignatureCheck.VALID_SIGNATURE_ONLY
    )
    assert evidence.grant_status == "active"
    assert b'"domain":"arbora.a2a.pending-call"' in evidence.binding.signing_bytes()
    assert b'"version":1' in evidence.binding.signing_bytes()


@pytest.mark.parametrize("field", ["task_id", "pending_call_id", "plan_ref"])
def test_request_identity_must_match_pending_binding(signed: Any, field: str) -> None:
    confirmation, evidence = signed
    changed = confirmation.model_copy(update={field: "changed"})
    assert (
        verify_pending_signature(changed, evidence=evidence, now_ms=1_500)
        == SignatureCheck.BINDING_MISMATCH
    )


@pytest.mark.parametrize(
    "field",
    [
        "parent_work_item_id",
        "child_work_item_id",
        "grant_id",
        "grant_revision",
        "expires_at_ms",
    ],
)
def test_signature_covers_every_pending_identity(signed: Any, field: str) -> None:
    confirmation, evidence = signed
    value = (
        3
        if field == "grant_revision"
        else 2_001
        if field == "expires_at_ms"
        else "changed"
    )
    changed = evidence.binding.model_copy(update={field: value})
    assert (
        verify_pending_signature(
            confirmation, evidence=replace(evidence, binding=changed), now_ms=1_500
        )
        == SignatureCheck.INVALID_SIGNATURE
    )


@pytest.mark.parametrize(
    "field", ["op_id", "params_digest", "registry_digest", "effect"]
)
def test_signature_covers_existing_plan_binding(signed: Any, field: str) -> None:
    confirmation, evidence = signed
    plan = replace(evidence.binding.plan, **{field: params_digest({"changed": True})})
    binding = evidence.binding.model_copy(update={"plan": plan})
    assert (
        verify_pending_signature(
            confirmation, evidence=replace(evidence, binding=binding), now_ms=1_500
        )
        == SignatureCheck.INVALID_SIGNATURE
    )


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"principal": "person:other"}, SignatureCheck.BINDING_MISMATCH),
        ({"tenant": "tenant:other"}, SignatureCheck.BINDING_MISMATCH),
        ({"policy_revision": "revision:other"}, SignatureCheck.BINDING_MISMATCH),
        ({"principal_kind": "service"}, SignatureCheck.HUMAN_REQUIRED),
        ({"delegated": True}, SignatureCheck.HUMAN_REQUIRED),
    ],
)
def test_current_human_must_match(
    signed: Any, changes: Any, expected: SignatureCheck
) -> None:
    confirmation, evidence = signed
    assert (
        verify_pending_signature(
            confirmation,
            evidence=replace(evidence, caller=caller(**changes)),
            now_ms=1_500,
        )
        == expected
    )


@pytest.mark.parametrize("now_ms", [999, 2_000, 2_001])
def test_expiry_boundaries_refuse(signed: Any, now_ms: int) -> None:
    confirmation, evidence = signed
    assert (
        verify_pending_signature(confirmation, evidence=evidence, now_ms=now_ms)
        == SignatureCheck.EXPIRED
    )


@pytest.mark.parametrize("status", ["consumed", "revoked", "expired", "unknown"])
def test_inactive_supplied_snapshot_refuses_without_claiming_atomicity(
    signed: Any, status: str
) -> None:
    confirmation, evidence = signed
    assert (
        verify_pending_signature(
            confirmation, evidence=replace(evidence, grant_status=status), now_ms=1_500
        )
        == SignatureCheck.GRANT_INACTIVE
    )


@pytest.mark.parametrize("signature", ["bad!", "a" * 88])
def test_malformed_or_invalid_signature_refuses(signed: Any, signature: str) -> None:
    confirmation, evidence = signed
    assert (
        verify_pending_signature(
            confirmation.model_copy(update={"signature": signature}),
            evidence=evidence,
            now_ms=1_500,
        )
        == SignatureCheck.INVALID_SIGNATURE
    )


def test_wrong_key_and_signer_refuse(signed: Any) -> None:
    confirmation, evidence = signed
    wrong_key = Ed25519PrivateKey.generate().public_key()
    assert (
        verify_pending_signature(
            confirmation, evidence=replace(evidence, public_key=wrong_key), now_ms=1_500
        )
        == SignatureCheck.INVALID_SIGNATURE
    )
    assert (
        verify_pending_signature(
            confirmation,
            evidence=replace(evidence, signer="person:other"),
            now_ms=1_500,
        )
        == SignatureCheck.BINDING_MISMATCH
    )


def test_binding_is_closed_frozen_and_bounded(signed: Any) -> None:
    _, evidence = signed
    with pytest.raises(ValidationError):
        evidence.binding.task_id = "changed"
    fields = dict(evidence.binding)
    malformed: list[dict[str, Any]] = [
        {"extra": True},
        {"version": 2},
        {"domain": "other"},
        {"expires_at_ms": 999},
        {"grant_revision": 0},
        {"approving_human": "other"},
    ]
    for updates in malformed:
        with pytest.raises(ValidationError):
            PendingCallBinding(**(fields | updates))


def test_route_reaches_verification_refusal_without_accepting_wire_authority(
    signed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arbora.a2a.approval as approval

    observed = []

    def checked(confirmation: Any, *, evidence: Any) -> SignatureCheck:
        observed.append(evidence)
        return verify_pending_signature(confirmation, evidence=evidence)

    monkeypatch.setattr(approval, "verify_pending_signature", checked)
    app, _, authority = _app()
    confirmation, _ = signed
    response = TestClient(app).post(
        "/a2a",
        headers={"Authorization": "Bearer verified"},
        json={
            "jsonrpc": "2.0",
            "id": "confirm",
            "method": "graphos.plan/confirm",
            "params": confirmation.model_dump(),
        },
    )
    assert response.status_code == 501
    assert response.json()["error"]["code"] == -32011
    assert "AUTHORITY_UNAVAILABLE" in response.json()["error"]["message"]
    assert observed == [None]
    assert authority.task.status.state == "submitted"
    with pytest.raises(ValidationError):
        A2APlanConfirmParams(**(confirmation.model_dump() | {"public_key": "wire-key"}))


@pytest.mark.parametrize("field", ["params_digest", "registry_digest"])
@pytest.mark.parametrize(
    "digest", ["", "not-a-digest", "a" * 63, "a" * 65, "A" * 64, "g" * 64]
)
def test_binding_rejects_malformed_digest(signed: Any, field: str, digest: str) -> None:
    _, evidence = signed
    fields = dict(evidence.binding)
    fields["plan"] = replace(evidence.binding.plan, **{field: digest})
    with pytest.raises(ValidationError):
        PendingCallBinding(**fields)
