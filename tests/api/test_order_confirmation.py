"""Confirmed order, position and wallet changes over structural SDK doubles (ARBORA-DATA-MARKET-R007).

The SDK wallet contract is not in the pinned release, so these tests install a structural
double of ``scion_sdk.writeback.wallet_intent`` (the shapes on scion-sdk main) and drive the
handlers with recording ports. A pinned release without the contract refuses UNAVAILABLE.
"""

# ruff: noqa: F811
# The served fixture is imported for pytest and re-requested by name, the repo idiom.

from __future__ import annotations

import hashlib
import json
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from arbora.api.domain.finance.changes import OrderChange, WalletOperation, refusal_for
from arbora.api.invoke.pipeline import EXTERNAL_EFFECT_OPERATIONS, OperationRefused
from arbora.api.ops import finance_changes as ops
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Confirm, Idempotency, Surface
from arbora.security.audit_event import audit_event
from tests.api._served_support import confirmable  # noqa: F401
from tests.test_served_operation_api import served  # noqa: F401

NO_OP: Any = None
INTENT = {
    "operation_id": "pay",
    "account_id": "acct-1",
    "wallet_id": "w1",
    "network_id": "xrpl",
    "key_id": "k1",
    "params": {"amount": "5"},
}


class Intent:
    def __init__(self, **fields: Any) -> None:
        for name in ("operation_id", "account_id", "wallet_id", "network_id", "key_id"):
            setattr(self, name, fields[name])
        self.params = fields.get("params", {})


def digest(intent: Intent) -> str:
    body = json.dumps(vars(intent), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = ModuleType("scion_sdk.writeback.wallet_intent")
    module.WalletBusinessIntent = Intent  # type: ignore[attr-defined]
    module.WalletConfirmation = dict  # type: ignore[attr-defined]
    module.WalletExecutionEnvelope = dict  # type: ignore[attr-defined]
    module.wallet_plan_digest = digest  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scion_sdk.writeback.wallet_intent", module)
    return module


class Wallet:
    """Recording ports; every provider mutation is one ``applied`` entry."""

    def __init__(self, verdict: dict[str, Any] | None = None, allowed: bool = True):
        self.verdict = verdict or {
            "outcome": "verified",
            "proof_digest": "p" * 64,
            "signer": "key-1",
            "trust_revision": "7",
            "signature": "SECRET",
        }
        self.allowed = allowed
        self.applied: list[tuple[dict[str, str], str | None]] = []
        self.proofs: list[Any] = []
        self.reconciled: list[Any] = []
        self.state = "applied"

    async def verify(self, projection: Any, proof: Any) -> Any:
        self.proofs.append(proof)
        return self.verdict

    async def authorize(self, projection: Any) -> Any:
        return {"allowed": self.allowed, "authorization_id": "auth-1"}

    async def apply(self, projection: Any, key: str | None) -> Any:
        self.applied.append((projection, key))
        return {"state": self.state, "effect_receipt": f"eff:{key}"}

    async def reconcile(self, projection: Any, key: str | None) -> None:
        self.reconciled.append(key)


NO_ENVELOPE: dict[str, Any] = {}


def context(
    wallet: Wallet | None = None,
    envelope: dict[str, Any] = NO_ENVELOPE,
    **services: Any,
) -> Any:
    bound = dict(services)
    if wallet is not None:
        bound["wallet_operations"] = wallet
    return SimpleNamespace(
        caller=SimpleNamespace(tenant="t1", principal="actor-1", request_id="r1"),
        services=bound,
        idempotency_key="key-1",
        envelope=envelope,
    )


def confirmed(params: dict[str, Any] = INTENT) -> dict[str, Any]:
    return {
        "intent": params,
        "confirmation": {
            "confirmed_by": "actor-1",
            "confirmed_at_ms": 1,
            "plan_digest": digest(Intent(**params)),
        },
    }


@pytest.mark.spec("ARBORA-DATA-MARKET-R007.1")
def test_models_bind_exact_intent_and_exclude_proof(sdk: ModuleType) -> None:
    wallet = WalletOperation.from_params("t1", "actor-1", INTENT)
    projection = wallet.projection()
    assert projection == {
        "tenant_id": "t1",
        "actor": "actor-1",
        "account_id": "acct-1",
        "network_id": "xrpl",
        "wallet_id": "w1",
        "operation_id": "pay",
        "plan_digest": digest(Intent(**INTENT)),
    }
    assert "proof" not in json.dumps(projection) and "signature" not in str(projection)
    order = OrderChange(
        tenant_id="t1", actor="a", account_id="acct-1", order_id="o1",
        action="amend", quantity="1.50",
    )  # fmt: skip
    assert order.identity() == "order:t1:acct-1:o1:amend:1.50:-"


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R007.2")
async def test_digest_is_stable_across_signing_and_retries_reuse_effect_key(
    sdk: ModuleType,
) -> None:
    port = Wallet()
    unsigned = await ops.handle_wallet_operate(context(port), confirmed(), NO_OP)
    signed = await ops.handle_wallet_operate(
        context(port, {"proof": {"signature": "SECRET"}}), confirmed(), NO_OP
    )
    assert unsigned["value"]["plan_digest"] == signed["value"]["plan_digest"]
    # Both calls carry the one effect key the pipeline gave them; arbora mints no nonce.
    assert [key for _p, key in port.applied] == ["key-1", "key-1"]
    assert not hasattr(ops, "NONCES")


@pytest.mark.spec("ARBORA-DATA-MARKET-R007.3")
def test_operations_are_registered_and_envelope_is_shared_over_http_and_mcp() -> None:
    registry = get_registry()
    for op_id in ("orders.change", "positions.change", "wallet.operate"):
        spec = registry.get(op_id)
        assert spec is not None
        assert spec.confirm is Confirm.PLAN
        assert spec.idempotency is Idempotency.KEY_REQUIRED
        assert {Surface.HTTP, Surface.MCP} <= set(spec.surfaces)
        assert op_id in EXTERNAL_EFFECT_OPERATIONS
    # The proof travels in the envelope of the one pipeline both routes call.
    assert registry.get("wallet.operate").envelope == ("proof",)
    assert set(ops.WalletOperateParams.model_fields) == {
        "intent",
        "confirmation",
        "proof",
    }


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R007.3")
async def test_wallet_is_unavailable_until_every_binding_exists(
    sdk: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(OperationRefused) as unbound:
        await ops.handle_wallet_operate(context(), confirmed(), NO_OP)
    assert unbound.value.code == "UNAVAILABLE"
    monkeypatch.delitem(sys.modules, "scion_sdk.writeback.wallet_intent")
    monkeypatch.setitem(sys.modules, "scion_sdk.writeback", None)
    with pytest.raises(OperationRefused) as pinned:
        await ops.handle_wallet_operate(context(Wallet()), confirmed(), NO_OP)
    assert pinned.value.code == "UNAVAILABLE"
    assert "wallet_intent" in str(pinned.value.details)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R007.4")
async def test_each_refusal_code_reaches_no_provider_mutation(sdk: ModuleType) -> None:
    cases: list[tuple[str, Wallet, dict[str, Any]]] = [
        ("CONFIRMATION_REQUIRED", Wallet(), {"intent": INTENT}),
        (
            "PLAN_MISMATCH",
            Wallet(),
            {**confirmed(), "intent": {**INTENT, "wallet_id": "w2"}},
        ),
        ("POLICY_DENIED", Wallet({"outcome": "refused", "reason": "bad_signature"}), confirmed()),
        ("POLICY_DENIED", Wallet(allowed=False), confirmed()),
        ("UNAVAILABLE", Wallet({"outcome": "refused", "reason": "verifier_absent"}), confirmed()),
    ]  # fmt: skip
    for code, port, params in cases:
        with pytest.raises(OperationRefused) as refused:
            await ops.handle_wallet_operate(context(port), params, NO_OP)
        assert refused.value.code == code
        assert port.applied == []
    assert refusal_for("bad_signature") == (
        "POLICY_DENIED",
        {"reason": "bad_signature"},
    )
    # The SDK typed reason survives in the redacted detail.
    with pytest.raises(OperationRefused) as detail:
        await ops.handle_wallet_operate(
            context(Wallet({"outcome": "refused", "reason": "key_revoked"})),
            confirmed(),
            NO_OP,
        )
    assert detail.value.details == {"reason": "key_revoked"}


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R007.4")
async def test_an_uncertain_prior_effect_enters_reconciliation_and_is_not_retried(
    sdk: ModuleType,
) -> None:
    port = Wallet()
    port.state = "uncertain"
    out = await ops.handle_wallet_operate(context(port), confirmed(), NO_OP)
    assert out["value"]["state"] == "reconciling"
    assert port.reconciled == ["key-1"] and len(port.applied) == 1


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R007.5")
async def test_audit_joins_authorization_and_proof_digest_without_secrets(
    sdk: ModuleType,
) -> None:
    out = await ops.handle_wallet_operate(context(Wallet()), confirmed(), NO_OP)
    spec = get_registry().get("wallet.operate")
    caller = SimpleNamespace(tenant="t1", principal="actor-1", request_id="r1")
    event = audit_event(
        spec, confirmed(), caller, Surface.HTTP, "ok", evidence=out["evidence"]
    )
    assert event["authorization_id"] == "auth-1"
    assert event["proof_digest"] == "p" * 64
    assert event["signer"] == "key-1" and event["trust_revision"] == "7"
    assert event["effect_receipt"] == "eff:key-1"
    assert "SECRET" not in json.dumps(event) and "signature" not in event


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R007.6")
async def test_wallet_proof_confirmation_and_replay(sdk: ModuleType) -> None:
    port = Wallet()
    params = confirmed()
    signed = {"proof": {"signature": "SECRET"}}
    first = await ops.handle_wallet_operate(context(port, signed), params, NO_OP)
    # A replay carries the same effect key, so the port reads the one receipt.
    again = await ops.handle_wallet_operate(context(port, signed), params, NO_OP)
    assert first["value"]["effect_receipt"] == again["value"]["effect_receipt"]
    assert {key for _p, key in port.applied} == {"key-1"}
    for altered in ("account_id", "network_id", "key_id"):
        with pytest.raises(OperationRefused) as refused:
            await ops.handle_wallet_operate(
                context(port), {**params, "intent": {**INTENT, altered: "x"}}, NO_OP
            )
        assert refused.value.code == "PLAN_MISMATCH"


class Doors:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def send_main(
        self, _context: Any, method: str, params: dict[str, Any], **kwargs: Any
    ) -> Any:
        self.sent.append((method, params, kwargs))
        return {"proposal_id": "pr-1", "revision": 1}


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R007.7")
async def test_stage_confirm_promote_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doors = Doors()
    monkeypatch.setattr(ops, "send_main", doors.send_main)
    staged = await ops.handle_order_stage(
        context(),
        {"snapshot": {"proposal_id": "pr-1"}, "expected_head_revision": None},
        NO_OP,
    )
    assert staged["value"]["proposal_id"] == "pr-1"
    assert doors.sent[0][0] == "FinanceOrderStage"
    assert doors.sent[0][1] == {"snapshot": {"proposal_id": "pr-1"}}
    receipt = {"gate_id": "g1", "digest": "d"}
    await ops.handle_order_confirm(
        context(),
        {
            "proposal_id": "pr-1",
            "candidate_revision": 1,
            "snapshot_digest": "s",
            "required_gate_manifest_digest": "m",
            "manifest_gate_ids": ["g1"],
            "gate_receipts": [receipt],
        },
        NO_OP,
    )
    method, params, _kw = doors.sent[1]
    assert method == "FinanceOrderPromote"
    # The human confirmation is the verified caller; the tenant is never a request field.
    assert params["request"]["approver"] == "actor-1"
    assert params["request"]["tenant_id"] == "t1"
    assert params["request"]["gate_receipts"] == [receipt]


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R007.7")
async def test_the_proposal_door_refuses_unavailable_on_a_release_without_it() -> None:
    with pytest.raises(OperationRefused) as refused:
        await ops.handle_order_stage(context(), {"snapshot": {}}, NO_OP)
    assert refused.value.code == "UNAVAILABLE"
    assert "FinanceOrderStage" in str(refused.value.details)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R007.3")
async def test_order_and_position_changes_need_the_composed_door() -> None:
    params = {"order_id": "o1", "action": "cancel", "account_id": "acct-1"}
    with pytest.raises(OperationRefused) as refused:
        await ops.handle_orders_change(context(), params, NO_OP)
    assert refused.value.code == "UNAVAILABLE"
    sent: list[Any] = []

    class Door:
        async def apply(self, *args: Any) -> Any:
            sent.append(args)
            return {"state": "applied"}

    out = await ops.handle_orders_change(
        context(finance_change_port=Door()), params, NO_OP
    )
    assert out["value"]["identity"] == "order:t1:acct-1:o1:cancel:-:-"
    assert sent[0][1] == "order" and sent[0][4] == "key-1"
    with pytest.raises(OperationRefused) as bad:
        await ops.handle_orders_change(
            context(finance_change_port=Door()), {**params, "action": "amend"}, NO_OP
        )
    assert bad.value.code == "INVALID_ARGUMENT"


@pytest.mark.spec("ARBORA-DATA-MARKET-R007.2")
def test_the_plan_digest_is_the_same_with_and_without_the_proof() -> None:
    from arbora.api.invoke.pipeline import split_envelope
    from arbora.api.invoke.plan import bind_plan
    from arbora.core.claims import params_digest

    op = get_registry().get("wallet.operate")
    caller = SimpleNamespace(
        principal="actor-1", tenant="t1", policy_revision="p1", request_id="r1"
    )
    unsigned = confirmed()
    signed = {**unsigned, "proof": {"signature": "SECRET"}}
    kept_unsigned, extension_unsigned = split_envelope(op, unsigned)
    kept_signed, extension = split_envelope(op, signed)
    # The proof is separated before hashing: the resubmit with the proof matches the plan.
    assert extension == {"proof": {"signature": "SECRET"}} and extension_unsigned == {}
    assert "proof" not in kept_signed
    assert params_digest(kept_signed) == params_digest(kept_unsigned)
    assert bind_plan(op, kept_signed, caller, "d") == bind_plan(
        op, kept_unsigned, caller, "d"
    )
    # An op with no declared envelope is untouched.
    other = get_registry().get("orders.change")
    assert split_envelope(other, signed) == (signed, {})


PROPOSAL = {"snapshot": {"proposal_id": "pr-1"}}
PROMOTION = {
    "proposal_id": "pr-1",
    "candidate_revision": 1,
    "snapshot_digest": "s",
    "required_gate_manifest_digest": "m",
    "manifest_gate_ids": ["g1"],
    "gate_receipts": [{"gate_id": "g1", "digest": "d"}],
}
OPS = ("market.order.stage", "market.order.confirm")


def _served_order_journey(
    monkeypatch: pytest.MonkeyPatch, served: Any, confirmable: dict[str, Any]
) -> tuple[Any, list[tuple[str, dict[str, Any], str]]]:
    from tests.api._market_support import MarketServed

    sent: list[tuple[str, dict[str, Any], str]] = []

    async def door(context: Any, method: str, params: dict[str, Any], **_k: Any) -> Any:
        sent.append((method, params, str(context.caller.principal)))
        return {"proposal_id": "pr-1", "revision": 1}

    monkeypatch.setattr(ops, "send_main", door)
    market = MarketServed(
        confirmable, served, monkeypatch, {}, scopes=("finance:propose-order",)
    )
    return market, sent


def _http_planned(market: Any, op_id: str, params: dict[str, Any], key: str) -> Any:
    """Preview, then resubmit under the confirmed plan: the one confirmation protocol."""
    preview = market.http(op_id, params, **{"Idempotency-Key": key})
    ref = preview.json()["error"]["details"]["plan_ref"]
    return market.http(
        op_id, params, **{"Idempotency-Key": key, "Arbora-Plan-Ref": ref}
    )


def _mcp_planned(market: Any, op_id: str, params: dict[str, Any], key: str) -> Any:
    preview = market.mcp(op_id, params, verb="act", idempotency_key=key)
    assert preview["ok"] is True, preview
    ref = preview["meta"]["plan"]["plan_ref"]
    return market.mcp(op_id, params, verb="act", idempotency_key=key, plan_ref=ref)


def _previews_leave_no_engine_call(market: Any, sent: list[Any]) -> None:
    for op_id, params in zip(OPS, (PROPOSAL, PROMOTION), strict=True):
        assert get_registry().get(op_id).confirm is Confirm.PLAN
        before = len(sent)
        preview = market.http(op_id, params, **{"Idempotency-Key": "k-preview"})
        assert preview.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
        assert len(sent) == before


def _both_doors_under_a_plan(market: Any) -> tuple[list[Any], list[Any]]:
    pairs = list(zip(OPS, (PROPOSAL, PROMOTION), strict=True))
    over_http = [
        _http_planned(market, op_id, params, f"http-{i}")
        for i, (op_id, params) in enumerate(pairs)
    ]
    over_mcp = [
        _mcp_planned(market, op_id, params, f"mcp-{i}")
        for i, (op_id, params) in enumerate(pairs)
    ]
    return over_http, over_mcp


def _both_doors_run_once_each(market: Any, sent: list[Any]) -> None:
    over_http, over_mcp = _both_doors_under_a_plan(market)
    assert [r.status_code for r in over_http] == [200, 200]
    assert [answer["ok"] for answer in over_mcp] == [True, True]
    assert [m for m, _p, _w in sent] == ["FinanceOrderStage", "FinanceOrderPromote"] * 2


def _a_tenant_field_is_refused(market: Any) -> None:
    refused = market.http("market.order.confirm", {**PROMOTION, "tenant_id": "x"})
    assert refused.status_code == 400


def _promotions_name_the_verified_approver(sent: list[Any]) -> None:
    from tests.api._market_support import plain

    promoted = [
        (plain(wire)["request"], principal)
        for method, wire, principal in sent
        if method == "FinanceOrderPromote"
    ]
    assert len(promoted) == 2
    for request, principal in promoted:
        # The verified caller is the approver; the tenant is never a request field.
        assert request["approver"] == principal == "person:one"
        assert request["tenant_id"] == "tenant:one"
        assert request["gate_receipts"] == PROMOTION["gate_receipts"]


@pytest.mark.spec("ARBORA-DATA-MARKET-R007.7")
def test_stage_confirm_promote(
    confirmable: dict[str, Any],
    served: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market, sent = _served_order_journey(monkeypatch, served, confirmable)
    _previews_leave_no_engine_call(market, sent)
    _both_doors_run_once_each(market, sent)
    _promotions_name_the_verified_approver(sent)
    _a_tenant_field_is_refused(market)


@pytest.fixture
def wallet_bound(
    confirmable: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], Wallet]:
    """Compose the recording wallet authority before ``served`` builds the app."""
    from tests.api._market_support import capture_with_bindings

    wallet = Wallet()
    return capture_with_bindings(monkeypatch, {"wallet_operations": wallet}), wallet


@pytest.mark.spec("ARBORA-DATA-MARKET-R007.3")
def test_the_proof_envelope_reaches_the_handler_over_http_and_mcp(
    sdk: ModuleType,
    wallet_bound: tuple[dict[str, Any], Wallet],
    served: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.api._market_support import MarketServed

    captured, wallet = wallet_bound
    market = MarketServed(
        captured, served, monkeypatch, {}, scopes=("finance:propose-order",)
    )
    body = confirmed()
    proof = {"signature": "SECRET"}
    # HTTP: the plan is previewed without the proof; the resubmit adds it and keeps the plan.
    key = {"Idempotency-Key": "wallet-http"}
    preview = market.http("wallet.operate", body, **key)
    ref = preview.json()["error"]["details"]["plan_ref"]
    signed = market.http(
        "wallet.operate", {**body, "proof": proof}, **key, **{"Arbora-Plan-Ref": ref}
    )
    assert signed.status_code == 200, signed.text
    # MCP: the same envelope over the same pipeline.
    shown = market.mcp("wallet.operate", body, verb="act", idempotency_key="wallet-mcp")
    mcp_ref = shown["meta"]["plan"]["plan_ref"]
    answered = market.mcp(
        "wallet.operate",
        {**body, "proof": proof},
        verb="act",
        idempotency_key="wallet-mcp",
        plan_ref=mcp_ref,
    )
    assert answered["ok"] is True, answered
    assert wallet.proofs == [proof, proof]
    assert len(wallet.applied) == 2
    assert "SECRET" not in json.dumps(preview.json())
