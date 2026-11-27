"""ARBORA-FLEET-R003.5 to R003.10: the served repository write-back profile."""

from __future__ import annotations

import copy

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import write_back as wb
from tests.fleet._repository_effect_support import (
    FakeGateway,
    binding_for,
    context,
    make_request,
    params,
    receipt_for,
)

_OP = None


async def _commit(ctx, payload):
    return await wb.handle_write_back_commit(ctx, payload, _OP)


async def _preview(ctx, payload):
    return await wb.handle_write_back_preview(ctx, payload, _OP)


def _good():
    request = make_request()
    binding = binding_for(request)
    gateway = FakeGateway({"value": {"receipt": receipt_for(request)}})
    ctx = context(bindings=wb.WriteBackBindings((binding,)), gateway=gateway)
    return request, binding, gateway, ctx


@pytest.mark.spec("ARBORA-FLEET-R003.5")
async def test_request_binding() -> None:
    request, binding, _gateway, ctx = _good()
    shown = await _preview(ctx, params(request, binding))
    assert shown["value"]["applied"] is False
    assert shown["value"]["payload_sha256"] == request.intent.payload_sha256

    unknown = params(request, binding)
    unknown["request"] = copy.deepcopy(unknown["request"])
    unknown["request"]["intent"]["extra"] = 1
    with pytest.raises(OperationRefused) as refused:
        await _preview(ctx, unknown)
    assert refused.value.code == "WRITE_BACK_UNKNOWN_FIELD"

    mismatch = params(request, binding)
    mismatch["request"] = copy.deepcopy(mismatch["request"])
    mismatch["request"]["intent"]["target_ref"] = "refs/heads/other"
    with pytest.raises(OperationRefused) as refused:
        await _preview(ctx, mismatch)
    assert refused.value.code == "WRITE_BACK_INTENT_MISMATCH"

    changed = {**params(request, binding), "binding_digest": "0" * 64}
    with pytest.raises(OperationRefused) as refused:
        await _preview(ctx, changed)
    assert refused.value.code == "WRITE_BACK_BINDING_CHANGED"


@pytest.mark.spec("ARBORA-FLEET-R003.6")
async def test_trusted_composition() -> None:
    request, binding, gateway, ctx = _good()
    result = await _commit(ctx, params(request, binding))
    assert result["value"]["applied"] is True
    assert [call[:2] for call in gateway.calls] == [
        ("repository-connector", "apply_repository_effect")
    ]

    for bindings in (None, wb.WriteBackBindings()):
        gateway = FakeGateway({"value": {"receipt": receipt_for(request)}})
        bare = context(bindings=bindings, gateway=gateway)
        with pytest.raises(OperationRefused) as refused:
            await _commit(bare, params(request, binding))
        assert refused.value.code == "WRITE_BACK_PRODUCER_MISSING"
        assert gateway.calls == []

    no_gateway = context(bindings=wb.WriteBackBindings((binding,)), gateway=None)
    with pytest.raises(OperationRefused) as refused:
        await _commit(no_gateway, params(request, binding))
    assert refused.value.code == "WRITE_BACK_PRODUCER_MISSING"


@pytest.mark.spec("ARBORA-FLEET-R003.6")
async def test_config_change_between_resolution_and_dispatch_refuses() -> None:
    request, binding, gateway, _ctx = _good()

    class Flipping(wb.WriteBackBindings):
        def __init__(self) -> None:
            super().__init__((binding,))
            self.reads = 0

        def find(self, *args):
            self.reads += 1
            return binding if self.reads == 1 else binding_for(request, tool="other")

    ctx = context(bindings=Flipping(), gateway=gateway)
    with pytest.raises(OperationRefused) as refused:
        await _commit(ctx, params(request, binding))
    assert refused.value.code == "WRITE_BACK_CONFIG_CHANGED"
    assert gateway.calls == []


@pytest.mark.spec("ARBORA-FLEET-R003.7")
async def test_commit_binding() -> None:
    request, binding, gateway, _ctx = _good()
    for key in (None, "", "plain-key"):
        ctx = context(
            bindings=wb.WriteBackBindings((binding,)), gateway=gateway, key=key
        )
        with pytest.raises(OperationRefused) as refused:
            await _commit(ctx, params(request, binding))
        assert refused.value.code == "WRITE_BACK_PLAN_UNBOUND"

    changed = {**params(request, binding), "binding_digest": "1" * 64}
    ctx = context(bindings=wb.WriteBackBindings((binding,)), gateway=gateway)
    with pytest.raises(OperationRefused) as refused:
        await _commit(ctx, changed)
    assert refused.value.code == "WRITE_BACK_BINDING_CHANGED"
    assert gateway.calls == []  # zero provider mutations for each binding change


@pytest.mark.spec("ARBORA-FLEET-R003.8")
async def test_actual_effect_readback() -> None:
    request, binding, _gateway, ctx = _good()
    result = (await _commit(ctx, params(request, binding)))["value"]
    assert result["effect_revision"] == "e" * 40
    assert result["request_digest"] == request.change_set.change_set_digest
    assert len(result["receipt_digest"]) == 64

    for tampered in (
        receipt_for(request, readback_sha256="f" * 64),
        receipt_for(request, idempotency_key="another-key"),
    ):
        gateway = FakeGateway({"value": {"receipt": tampered}})
        ctx = context(bindings=wb.WriteBackBindings((binding,)), gateway=gateway)
        with pytest.raises(OperationRefused) as refused:
            await _commit(ctx, params(request, binding))
        assert refused.value.code == "WRITE_BACK_RECEIPT_UNBOUND"

    for echoed in ({"value": {"applied": True}}, {"value": {}}, None):
        gateway = FakeGateway(echoed)
        ctx = context(bindings=wb.WriteBackBindings((binding,)), gateway=gateway)
        with pytest.raises(OperationRefused) as refused:
            await _commit(ctx, params(request, binding))
        assert refused.value.code == "WRITE_BACK_RECEIPT_UNBOUND"


@pytest.mark.spec("ARBORA-FLEET-R003.9")
async def test_refusal_parity() -> None:
    from arbora.api.domain.fleet.service_child import ServiceChildOutcomeUnknown

    request, binding, _gateway, _ctx = _good()
    gateway = FakeGateway(error=ServiceChildOutcomeUnknown("readback failed"))
    ctx = context(bindings=wb.WriteBackBindings((binding,)), gateway=gateway)
    with pytest.raises(OperationRefused) as refused:
        await _commit(ctx, params(request, binding))
    assert refused.value.code == "WRITE_BACK_EFFECT_UNCERTAIN"
    assert len(gateway.calls) == 1  # one uncertain result, never a second write


@pytest.mark.spec("ARBORA-FLEET-R003.10")
async def test_restart_replay() -> None:
    request, binding, _gateway, _ctx = _good()
    gateway = FakeGateway(error=RuntimeError("connector restarted"))
    ctx = context(bindings=wb.WriteBackBindings((binding,)), gateway=gateway)
    with pytest.raises(OperationRefused) as refused:
        await _commit(ctx, params(request, binding))
    assert refused.value.code == "WRITE_BACK_REPLAY_UNRESOLVED"
    assert len(gateway.calls) == 1

    # A rebuilt process reads the same trusted bindings and recovers the proven
    # result of the same effect key; the connector's receipt is the proof.
    recovered = FakeGateway({"value": {"receipt": receipt_for(request)}})
    rebuilt = context(bindings=wb.WriteBackBindings((binding,)), gateway=recovered)
    result = await _commit(rebuilt, params(request, binding))
    assert result["value"]["receipt"]["idempotency_key"] == (
        request.change_set.idempotency_key
    )
