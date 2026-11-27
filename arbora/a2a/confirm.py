"""The A2A approval chain: ``input-required`` emission and ``arbora.plan/confirm`` dispatch.

ARBORA-A2A-R005.2.5, R005.5. A pending effect becomes a durable grant over the engine's
control leases (:class:`~arbora.a2a.grant_store.HumanGrantStore`) and its task projects as
``input-required`` with the server-attested binding. ``confirm_dispatch`` then verifies the
original human's detached signature over that binding, consumes the grant once and only
then runs the effect through the shared invoke. A valid signature, a key registration or a
grant snapshot alone never authorizes anything, and a refusal never reads as an approval.
Arbora keeps no approval state: every fact lives in the engine lease.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from arbora.api.invoke.plan import PLAN_TTL_MS, PlanBinding
from arbora.api.invoke.steps import VerifiedCaller

from .approval import (
    ApprovalVerificationEvidence,
    PendingCallBinding,
    SignatureCheck,
    verify_pending_signature,
)
from .grant_store import GrantError, GrantEvidence, HumanGrantStore
from .held_item_admission import HeldItemAdmissionRequest, HeldItemAdmitter
from .models import A2APlanConfirmParams, parse_confirmation
from .tool_action_admission import (
    ConfirmRefusal,
    ToolActionAdmissionRequest,
    ToolActionAdmitter,
    ToolActionRefusal,
)

UNQUALIFIED = "APPROVAL_CHAIN_UNQUALIFIED"


class ApprovalRefused(Exception):
    """One refusal code; the text names no caller input and no key material."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ApprovalKeyProducer(Protocol):
    """The registered approval key of one human, or ``None`` when none is registered.

    The key comes from the identity authority, never from request params. A key held by
    another tenant or another human answers ``None`` like an absent one.
    """

    async def public_key(self, tenant: str, human: str) -> Ed25519PublicKey | None: ...


class RegisteredKeyProducer:
    """The :class:`ApprovalKeyProducer` over the identity ``HumanApprovalKeyAuthority``.

    The authority resolves a key only by its registered owner and purpose. This adapter
    additionally refuses a key of another tenant or human, a revoked key and an expired
    one, so possession of key bytes never stands in for ownership.
    """

    def __init__(
        self,
        authority: Any,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self._authority = authority
        self._clock_ms = clock_ms

    @staticmethod
    def key_id_for(human: str) -> str:
        return f"approval-key:{human}"

    async def public_key(self, tenant: str, human: str) -> Ed25519PublicKey | None:
        try:
            key = await self._authority.resolve_key(
                self.key_id_for(human), principal_id=human, purpose="signed_approval"
            )
        except Exception:
            logging.getLogger(__name__).debug(
                "an unresolvable signing identity is absent", exc_info=True
            )
            return None
        if (
            key.tenant != tenant
            or key.principal_id != human
            or key.revoked_at_ms is not None
            or key.expires_at_ms <= self._clock_ms()
        ):
            return None
        return Ed25519PublicKey.from_public_bytes(bytes.fromhex(key.public_key_hex))


#: ``(binding) -> effect result``: the shared invoke of the approved operation, run once.
EffectRunner = Callable[[PendingCallBinding], Awaitable[Mapping[str, Any]]]
#: One audit entry per attempt: ``{grant_id, grant_revision, pending_call_id, outcome}``.
AuditSink = Callable[[Mapping[str, Any]], Awaitable[None]]


def grant_id_for(task_id: str, pending_call_id: str) -> str:
    """The deterministic grant id of one pending call on one task."""
    digest = hashlib.sha256(f"{task_id}\0{pending_call_id}".encode()).hexdigest()
    return f"grant:{digest}"


@dataclass(frozen=True, slots=True)
class ApprovalChain:
    """The composed chain; each seam is a real service, never a stand-in."""

    grants: HumanGrantStore
    keys: ApprovalKeyProducer
    effect: EffectRunner
    audit: AuditSink
    clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)  # noqa: E731
    #: The engine admitters of the two confirmation kinds; an absent one refuses.
    tool_actions: ToolActionAdmitter | None = None
    held_items: HeldItemAdmitter | None = None


async def emit_input_required(
    chain: ApprovalChain,
    *,
    task_id: str,
    parent_work_item_id: str,
    child_work_item_id: str,
    pending_call_id: str,
    plan_ref: str,
    plan: PlanBinding,
) -> dict[str, Any]:
    """Move a pending effect to ``input-required``: issue its durable grant.

    Returns the task status projection the A2A reads serve. The binding names the
    original human, so only that human's registered key can later approve it.
    """
    now = chain.clock_ms()
    binding = PendingCallBinding(
        task_id=task_id,
        parent_work_item_id=parent_work_item_id,
        child_work_item_id=child_work_item_id,
        pending_call_id=pending_call_id,
        plan_ref=plan_ref,
        plan=plan,
        approving_human=plan.principal,
        grant_id=grant_id_for(task_id, pending_call_id),
        grant_revision=1,
        issued_at_ms=now,
        expires_at_ms=now + PLAN_TTL_MS,
    )
    view = await chain.grants.issue(binding)
    return {
        "state": "input-required",
        "pending_call": {
            "task_id": task_id,
            "pending_call_id": pending_call_id,
            "plan_ref": plan_ref,
            "grant_id": view.grant_id,
            "grant_revision": view.revision,
            "expires_at_ms": binding.expires_at_ms,
            "signing_payload": binding.signing_bytes().decode("utf-8"),
        },
    }


async def _audit(
    chain: ApprovalChain,
    confirmation: A2APlanConfirmParams,
    evidence: GrantEvidence | None,
    outcome: str,
) -> None:
    await chain.audit(
        {
            "grant_id": evidence.grant_id
            if evidence
            else grant_id_for(confirmation.task_id, confirmation.pending_call_id),
            "grant_revision": evidence.grant_revision if evidence else 0,
            "pending_call_id": confirmation.pending_call_id,
            "outcome": outcome,
        }
    )


async def confirm_dispatch(
    raw_params: Any,
    *,
    caller: VerifiedCaller,
    chain: ApprovalChain | None,
) -> dict[str, Any]:
    """``arbora.plan/confirm``: verify, consume the grant once, then run the effect once.

    Every refusal happens before the effect and leaves one audit entry with the grant id
    and the refusal code.
    """
    confirmation = parse_confirmation(raw_params)
    if chain is None:
        raise ApprovalRefused(UNQUALIFIED)
    grant_id = grant_id_for(confirmation.task_id, confirmation.pending_call_id)
    try:
        view = await chain.grants.get(caller.tenant, grant_id)
        if view is None:
            # A foreign tenant's grant and an absent one answer alike.
            raise ApprovalRefused(SignatureCheck.BINDING_MISMATCH.value)
        if (
            isinstance(confirmation, ToolActionAdmissionRequest)
            and confirmation.params_digest != view.binding.plan.params_digest
        ):
            # The signed call is bound to its params; a changed digest is another call.
            raise ApprovalRefused(SignatureCheck.BINDING_MISMATCH.value)
        key = await chain.keys.public_key(caller.tenant, caller.principal)
        if key is None:
            raise ApprovalRefused(SignatureCheck.AUTHORITY_UNAVAILABLE.value)
        check = verify_pending_signature(
            confirmation,
            evidence=ApprovalVerificationEvidence(
                binding=view.binding,
                caller=caller,
                signer=caller.principal,
                public_key=key,
                grant_status=view.status,
            ),
            now_ms=chain.clock_ms(),
        )
        if check is not SignatureCheck.VALID_SIGNATURE_ONLY:
            raise ApprovalRefused(check.value)
        consumed = await chain.grants.consume(caller.tenant, grant_id)
    except GrantError as refusal:
        await _audit(chain, confirmation, None, refusal.code.value)
        raise ApprovalRefused(refusal.code.value) from None
    except ApprovalRefused as refusal:
        await _audit(chain, confirmation, None, refusal.code)
        raise
    await _audit(chain, confirmation, consumed, "consumed")
    admitted, proceed = await _admit(chain, confirmation, caller.tenant)
    result = await chain.effect(view.binding) if proceed else None
    return {
        "task_id": confirmation.task_id,
        "pending_call_id": confirmation.pending_call_id,
        "grant_id": consumed.grant_id,
        "grant_revision": consumed.grant_revision,
        "result": None if result is None else dict(result),
        "admission": admitted,
    }


async def _admit(
    chain: ApprovalChain, confirmation: A2APlanConfirmParams, tenant: str
) -> tuple[dict[str, Any], bool]:
    """Ask the engine; only an admit lets the effect run, and any refusal denies."""
    if isinstance(confirmation, ToolActionAdmissionRequest):
        if chain.tool_actions is None:
            raise ToolActionRefusal("ENGINE_UNAVAILABLE")
        approval = await chain.tool_actions.admit(confirmation, tenant=tenant)
        return approval.model_dump(mode="json", by_alias=True), (
            approval.verdict == "admit"
        )
    assert isinstance(confirmation, HeldItemAdmissionRequest)
    if chain.held_items is None:
        raise ConfirmRefusal("ENGINE_UNAVAILABLE")
    verdict = await chain.held_items.admit(confirmation, tenant=tenant)
    return verdict.model_dump(mode="json", by_alias=True, exclude_none=True), True
