"""Detached signature checks; no grant issuance, consumption or effect authority."""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from graph_os.api.invoke.plan import PLAN_TTL_MS, PlanBinding
from graph_os.api.invoke.steps import VerifiedCaller, authenticate

from .models import A2APlanConfirmParams


class PendingCallBinding(BaseModel):
    """Server-owned binding, never an authoritative object supplied on the wire."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    domain: Literal["graphos.a2a.pending-call"] = "graphos.a2a.pending-call"
    version: Literal[1] = 1
    task_id: str = Field(min_length=1, max_length=80)
    parent_work_item_id: str = Field(min_length=1, max_length=512)
    child_work_item_id: str = Field(min_length=1, max_length=512)
    pending_call_id: str = Field(min_length=1, max_length=256)
    plan_ref: str = Field(min_length=1, max_length=512)
    plan: PlanBinding
    approving_human: str = Field(min_length=1, max_length=512)
    grant_id: str = Field(min_length=1, max_length=512)
    grant_revision: int = Field(ge=1)
    issued_at_ms: int = Field(gt=0)
    expires_at_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def _valid_binding(self) -> PendingCallBinding:
        if not 0 < self.expires_at_ms - self.issued_at_ms <= PLAN_TTL_MS:
            raise ValueError("approval validity must fit the plan lifetime")
        if self.parent_work_item_id == self.child_work_item_id:
            raise ValueError("pending call requires distinct parent and child")
        if self.approving_human != self.plan.principal:
            raise ValueError("approval must name the original human")
        if self.plan.confirm != "plan":
            raise ValueError("A2A cannot confirm console-only operations")
        if any(
            not value.strip() or len(value) > 512
            for value in self.plan.as_grant().values()
        ):
            raise ValueError("plan bindings must be nonempty and bounded")
        if any(
            re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for digest in (self.plan.params_digest, self.plan.registry_digest)
        ):
            raise ValueError("plan digests must be lowercase SHA256 hex")
        return self

    def signing_bytes(self) -> bytes:
        """Canonical versioned JSON, including the domain and every binding field."""
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ApprovalVerificationEvidence:
    """Server-supplied snapshot, not proof of freshness or atomic consumption.

    A future composed authority must resolve the key's human ownership and read
    current durable evidence. No request payload may construct this object.
    """

    binding: PendingCallBinding
    caller: VerifiedCaller
    signer: str
    public_key: Ed25519PublicKey
    grant_status: str


class SignatureCheck(StrEnum):
    VALID_SIGNATURE_ONLY = "VALID_SIGNATURE_ONLY"
    AUTHORITY_UNAVAILABLE = "AUTHORITY_UNAVAILABLE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    GRANT_INACTIVE = "GRANT_INACTIVE"
    EXPIRED = "EXPIRED"
    INVALID_SIGNATURE = "INVALID_SIGNATURE"


def _binding_refusal(
    confirmation: A2APlanConfirmParams,
    evidence: ApprovalVerificationEvidence,
    now_ms: int,
) -> SignatureCheck | None:
    binding, caller = evidence.binding, evidence.caller
    if (
        authenticate(caller) is not None
        or caller.principal_kind != "human"
        or caller.delegated
    ):
        return SignatureCheck.HUMAN_REQUIRED
    expected = (binding.task_id, binding.pending_call_id, binding.plan_ref)
    actual = (confirmation.task_id, confirmation.pending_call_id, confirmation.plan_ref)
    human = (caller.principal, evidence.signer, binding.approving_human)
    if actual != expected or len(set(human)) != 1:
        return SignatureCheck.BINDING_MISMATCH
    if (caller.tenant, caller.policy_revision) != (
        binding.plan.tenant,
        binding.plan.policy_revision,
    ):
        return SignatureCheck.BINDING_MISMATCH
    if evidence.grant_status != "active":
        return SignatureCheck.GRANT_INACTIVE
    if not binding.issued_at_ms <= now_ms < binding.expires_at_ms:
        return SignatureCheck.EXPIRED
    return None


def verify_pending_signature(
    confirmation: A2APlanConfirmParams,
    *,
    evidence: ApprovalVerificationEvidence | None,
    now_ms: int | None = None,
) -> SignatureCheck:
    """Check evidence without granting permission or changing durable state."""
    if evidence is None:
        return SignatureCheck.AUTHORITY_UNAVAILABLE
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    refusal = _binding_refusal(confirmation, evidence, current_ms)
    if refusal is not None:
        return refusal
    try:
        signature = base64.b64decode(
            confirmation.signature, altchars=b"-_", validate=True
        )
        if len(signature) != 64:
            return SignatureCheck.INVALID_SIGNATURE
        evidence.public_key.verify(signature, evidence.binding.signing_bytes())
    except (InvalidSignature, ValueError, binascii.Error):
        return SignatureCheck.INVALID_SIGNATURE
    return SignatureCheck.VALID_SIGNATURE_ONLY
