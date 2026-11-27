"""The production wallet authority and change door (ARBORA-DATA-MARKET-R007.3).

``EngineWalletOperations`` is the ``wallet_operations`` port of ``wallet.operate``. It holds
no key, no nonce store and no approval store: the signature verdict is the SDK verifier over a
key the engine's trust record names, authorization is the engine's resource grant ledger, and
the durable effect reservation is one engine node created once per idempotency key. The
provider call is a governed door the host binds when the venue publishes the wallet tool;
until then the operation refuses ``UNAVAILABLE`` after authorization and before any
reservation, so nothing is half done. A pinned release without an SDK or engine shape refuses
naming it (PIN-LAG).

``FleetFinanceChangePort`` is the ``finance_change_port`` of ``orders.change`` and
``positions.change``: it reaches the venue only through the governed fleet call and only for
the changes the venue publishes a tool for.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import msgpack

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.pin_guards import guard
from arbora.api.ops.pin_guards import refusal as pin_refusal
from arbora.finance.delegation import DELEGATION_PROTOCOL
from arbora.pin_lag.scion_sdk import require_wallet_intent, require_wallet_trust
from arbora.pin_lag.unavailable import PinLagUnavailable

__all__ = ["EngineWalletOperations", "FleetFinanceChangePort", "reservation_id"]

#: ``(method, params)`` to the engine as the host service identity, answers decoded.
Wire = Callable[[str, dict[str, Any]], Awaitable[Any]]
#: ``() -> client``: an engine client for the resolver's trust read.
ClientFactory = Callable[[], Awaitable[Any]]
#: ``(projection, key) -> receipt``: the governed provider door.
ProviderDoor = Callable[[Mapping[str, str], str], Awaitable[Mapping[str, Any]]]


def reservation_id(tenant: str, key: str) -> str:
    return f"wallet-effect:{tenant}:{key}"


def _wallet_resource(projection: Mapping[str, str]) -> str:
    return f"wallet:{projection['network_id']}:{projection['wallet_id']}"


class EngineWalletOperations:
    """Verify, authorize, reserve and (through a bound door) apply one wallet operation."""

    def __init__(
        self,
        wire: Wire,
        client: ClientFactory,
        *,
        provider: ProviderDoor | None = None,
        clock_ms: Callable[[], int],
    ) -> None:
        self._wire = wire
        self._client = client
        self._provider = provider
        self._clock_ms = clock_ms

    async def verify(
        self, projection: Mapping[str, str], proof: Any
    ) -> Mapping[str, Any]:
        try:
            return await self._verify(projection, proof)
        except PinLagUnavailable as missing:
            raise pin_refusal(missing) from missing

    async def _verify(
        self, projection: Mapping[str, str], proof: Any
    ) -> Mapping[str, Any]:
        require_wallet_intent()
        trust = require_wallet_trust()
        from scion_sdk.writeback.wallet_proof import (
            WalletProofBinding,
            WalletProofRequest,
            WalletSignatureVerifier,
        )

        if not proof:
            return {"outcome": "refused", "reason": "missing_signature"}
        try:
            request = WalletProofRequest.model_validate(proof)
        except ValueError:
            return {"outcome": "refused", "reason": "invalid_payload"}
        binding = WalletProofBinding(
            tenant_id=projection["tenant_id"],
            actor_id=projection["actor"],
            connector_id=request.connector_id,
            operation_id=projection["operation_id"],
            account_id=projection["account_id"],
            wallet_id=projection["wallet_id"],
            network_id=projection["network_id"],
            key_id=request.key_id,
            plan_digest=projection["plan_digest"],
            change_set_digest=request.change_set_digest,
            change_set_id=request.nonce,
            idempotency_key=request.idempotency_key,
        )
        guard("WalletKeyTrustGet")
        verifier = WalletSignatureVerifier(
            trust.EngineWalletKeyResolver(await self._client())
        )
        result = await verifier.verify(request, binding, now=self._clock_ms() // 1000)
        return {
            "outcome": result.outcome,
            "reason": result.reason,
            "proof_digest": result.payload_digest,
            "signer": result.signer_ref,
            "trust_revision": result.trust_revision,
            # The signed key: the one effect this proof can ever authorize.
            "effect_key": request.idempotency_key,
        }

    async def authorize(self, projection: Mapping[str, str]) -> Mapping[str, Any]:
        """The engine's resource grant ledger decides; a verified signature is not enough."""
        try:
            guard("CheckResourceAccess")
        except PinLagUnavailable as missing:
            raise pin_refusal(missing) from missing
        answer = await self._wire(
            "CheckResourceAccess",
            {
                "agent_id": projection["actor"],
                "resource_ref": _wallet_resource(projection),
                "protocol": DELEGATION_PROTOCOL,
                "operation": projection["operation_id"],
            },
        )
        allowed = isinstance(answer, Mapping) and answer.get("allow") is True
        return {
            "allowed": allowed,
            "authorization_id": f"wallet-auth:{projection['plan_digest']}",
        }

    async def apply(
        self, projection: Mapping[str, str], key: str | None
    ) -> Mapping[str, Any]:
        if self._provider is None:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "the wallet connector tool is not published"}
            )
        if not key:
            raise OperationRefused("INVALID_ARGUMENT", {"field": "idempotency_key"})
        node = reservation_id(projection["tenant_id"], key)
        created = await self._wire(
            "CreateNodeIfAbsent",
            {
                "node_id": node,
                "properties_msgpack": msgpack.packb(
                    {
                        "type": "WalletEffectReservation",
                        "tenant_id": projection["tenant_id"],
                        "plan_digest": projection["plan_digest"],
                        "state": "reserved",
                    },
                    use_bin_type=True,
                ),
            },
        )
        if created is False:
            # A prior attempt reserved this key and its outcome is unknown: never retried blind.
            return {"state": "uncertain", "effect_receipt": node}
        receipt = dict(await self._provider(projection, key))
        return {"state": "applied", "effect_receipt": node, **receipt}

    async def reconcile(self, projection: Mapping[str, str], key: str | None) -> None:
        await self._wire(
            "CompareAndSetNodeFields",
            {
                "node_id": reservation_id(projection["tenant_id"], key or ""),
                "conditions_msgpack": msgpack.packb(
                    {"state": "reserved"}, use_bin_type=True
                ),
                "updates_msgpack": msgpack.packb(
                    {"state": "reconciling"}, use_bin_type=True
                ),
            },
        )


class FleetFinanceChangePort:
    """Order and position changes over the governed fleet call of the venue connector."""

    async def apply(
        self,
        context: Any,
        kind: str,
        identity: str,
        change: Mapping[str, Any],
        key: str | None,
    ) -> Mapping[str, Any]:
        from arbora.api.ops.finance import ORDERS_TOOL, _venue

        if kind != "order" or change.get("action") != "cancel":
            # The venue publishes a cancel tool only; an amend or position change has none.
            raise OperationRefused(
                "UNAVAILABLE", {"reason": f"the venue publishes no {kind} change tool"}
            )
        port = context.services.get("finance_read_port")
        bound = (
            await port.account_binding(context, change["tenant_id"]) if port else None
        )
        if bound != change["account_id"]:
            # A foreign account answers exactly like an absent order.
            raise OperationRefused("NOT_FOUND")
        answer = await _venue(
            context, ORDERS_TOOL, {"action": "cancel", "order_id": change["order_id"]}
        )
        return {"state": "applied", "venue": dict(answer)}
