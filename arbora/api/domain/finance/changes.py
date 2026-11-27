"""Confirmed order, position and wallet changes (ARBORA-DATA-MARKET-R007.1 and R007.4).

``OrderChange`` and ``PositionChange`` bind the exact change a caller confirms.
``WalletOperation`` carries the SDK's ``WalletBusinessIntent`` and takes its plan digest
from ``scion_sdk.writeback.wallet_intent.wallet_plan_digest``, so both repositories hash the
same bytes. Proof and confirmation metadata stay outside the business parameters: the
projection never embeds a proof request. The SDK contract is guarded by
``arbora.pin_lag.scion_sdk.require_wallet_intent``; a pinned release without it refuses
``UNAVAILABLE`` naming the symbol, and arbora has no fallback hash.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arbora.core.claims import claim_values

__all__ = [
    "OrderChange",
    "PositionChange",
    "WalletOperation",
    "refusal_for",
    "safe_evidence",
]

_DECIMAL = r"^-?\d+(\.\d+)?$"
#: SDK typed reasons mapped to one arbora refusal code. Anything else fails closed.
_REASON_CODES: Mapping[str, str] = {
    "missing_confirmation": "CONFIRMATION_REQUIRED",
    "plan_mismatch": "PLAN_MISMATCH",
    "scope_mismatch": "POLICY_DENIED",
    "missing_signature": "POLICY_DENIED",
    "bad_signature": "POLICY_DENIED",
    "key_revoked": "POLICY_DENIED",
    "expired": "POLICY_DENIED",
    "unentitled": "POLICY_DENIED",
    "verifier_absent": "UNAVAILABLE",
    "trust_absent": "UNAVAILABLE",
    "binding_absent": "UNAVAILABLE",
    "envelope_absent": "UNAVAILABLE",
}


class _Change(BaseModel):
    """The authenticated facts every change binds: tenant, actor and account."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    account_id: str = Field(min_length=1)


class OrderChange(_Change):
    order_id: str = Field(min_length=1, max_length=128)
    action: Literal["amend", "cancel"]
    quantity: str | None = Field(default=None, pattern=_DECIMAL)
    limit_price: str | None = Field(default=None, pattern=_DECIMAL)

    def identity(self) -> str:
        return ":".join(
            (
                "order",
                self.tenant_id,
                self.account_id,
                self.order_id,
                self.action,
                self.quantity or "-",
                self.limit_price or "-",
            )
        )


class PositionChange(_Change):
    position_id: str = Field(min_length=1, max_length=128)
    target_quantity: str = Field(pattern=_DECIMAL)

    def identity(self) -> str:
        return ":".join(
            (
                "position",
                self.tenant_id,
                self.account_id,
                self.position_id,
                self.target_quantity,
            )
        )


@dataclass(frozen=True, slots=True)
class WalletOperation:
    """One wallet intent bound to the authenticated tenant and actor."""

    tenant_id: str
    actor: str
    intent: Any

    @classmethod
    def from_params(
        cls, tenant_id: str, actor: str, params: Mapping[str, Any]
    ) -> WalletOperation:
        from arbora.pin_lag.scion_sdk import require_wallet_intent

        module = require_wallet_intent()
        # The served params are frozen; the strict SDK model takes plain JSON values.
        return cls(
            tenant_id, actor, module.WalletBusinessIntent(**claim_values(params))
        )

    def plan_digest(self) -> str:
        from arbora.pin_lag.scion_sdk import require_wallet_intent

        return str(require_wallet_intent().wallet_plan_digest(self.intent))

    def projection(self) -> dict[str, str]:
        """The immutable projection: no proof, no signature, no confirmation."""
        return {
            "tenant_id": self.tenant_id,
            "actor": self.actor,
            "account_id": self.intent.account_id,
            "network_id": self.intent.network_id,
            "wallet_id": self.intent.wallet_id,
            "operation_id": self.intent.operation_id,
            "plan_digest": self.plan_digest(),
        }


def refusal_for(reason: str) -> tuple[str, dict[str, str]]:
    """The arbora code and redacted detail for an SDK typed reason (fail closed)."""
    return _REASON_CODES.get(reason, "POLICY_DENIED"), {"reason": reason}


#: Evidence fields that may be joined to the audit record: identifiers only.
_EVIDENCE = (
    "authorization_id",
    "proof_digest",
    "signer",
    "trust_revision",
    "effect_receipt",
    "verification_outcome",
)


def safe_evidence(source: Mapping[str, Any]) -> dict[str, str]:
    """The joinable identifiers; a signature, key or proof body is never copied."""
    return {name: str(source[name]) for name in _EVIDENCE if source.get(name)}
