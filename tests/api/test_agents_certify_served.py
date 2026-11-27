"""``agents.certify`` is the separate certification-mode door (ARBORA-OPS-R872)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import certification
from arbora.api.registry import Effect
from arbora.pin_lag import medulla as pin_lag
from tests.api._engine_double import EngineDouble, context

_OP = certification.operations()[0]
_REQUEST = {"schema_version": 1, "skill": {"component_id": "s1"}}


@pytest.mark.spec("ARBORA-OPS-R872")
def test_certify_is_its_own_write_op_behind_the_certification_scope() -> None:
    assert _OP.id == "agents.certify" and _OP.effect is Effect.WRITE
    assert _OP.scopes == frozenset({"admin:decision-eval"})
    assert "agents.certify" in certification.NATIVE_IDEMPOTENCY
    from arbora.api.ops import agents

    assert "agents.certify" not in {op.id for op in agents.operations()}


@pytest.mark.spec("ARBORA-OPS-R872")
async def test_certify_stamps_the_verified_tenant_and_key_and_forwards_one_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = EngineDouble(monkeypatch)
    engine.serve(
        "DecisionEval",
        lambda p: {"attestation_digest": "d1"},
        "LocalCertificationRequest",
    )
    engine.install(pin_lag)
    answer = await certification.handle_agents_certify(
        context(key="idem-1"), {"request": dict(_REQUEST)}, _OP
    )
    assert answer == {"value": {"attestation_digest": "d1"}}
    [(method, params, idempotent)] = engine.sent
    assert method == "DecisionEval" and idempotent
    job = params["op"]
    assert job["op"] == "certify_local_skill"
    assert job["request"]["tenant_id"] == "tenant:one"
    assert job["request"]["idempotency_key"] == "idem-1"
    assert job["request"]["skill"] == {"component_id": "s1"}


@pytest.mark.spec("ARBORA-OPS-R872")
async def test_a_pin_without_the_request_shape_refuses_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medulla.generated import models

    monkeypatch.delattr(models, "LocalCertificationRequest", raising=False)
    with pytest.raises(OperationRefused) as refused:
        await certification.handle_agents_certify(
            context(), {"request": dict(_REQUEST)}, _OP
        )
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "LocalCertificationRequest"
