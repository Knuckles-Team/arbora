"""Technique activation is bound to technique-specific evidence (ARBORA-OPS-R054)."""

from __future__ import annotations

import pytest
from medulla import EngineResponseError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import serving_techniques
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Composite, Confirm
from tests.api._question_support import answered, served

TECHNIQUES = (
    "paged_cache",
    "prefix_reuse",
    "fused_kernels",
    "quantized_cache",
    "expert_dispatch",
)
_OP = serving_techniques.operations()[0]
DIGEST = "c" * 64


def _params(technique: str) -> dict:
    return {
        "technique": technique,
        "model_ref": "model:a",
        "configuration_digest": DIGEST,
        "receipts": [
            {"technique": technique, "model": "model:a", "config_digest": DIGEST}
        ],
    }


def _committed(engine: object) -> int:
    return [sent[0] for sent in engine.sent].count("DecisionCommit")  # type: ignore[attr-defined]


@pytest.mark.spec("ARBORA-OPS-R054")
@pytest.mark.spec("ARBORA-OPS-R054.2")
async def test_activation_requires_technique_bound_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op = get_registry()["serving.technique.activate"]
    assert op.confirm is Confirm.PLAN and op.scopes == frozenset(
        {"agent:decision-write"}
    )
    assert isinstance(op.binding, Composite)
    ctx, engine = served(monkeypatch, decide=lambda _p: answered("activate"))
    for technique in TECHNIQUES:
        result = await serving_techniques.handle_technique_activate(
            ctx, _params(technique), _OP
        )
        assert result["value"]["verdict"] == "activate"
        assert result["value"]["technique"] == technique
        assert result["value"]["record_digest"]
    assert _committed(engine) == len(TECHNIQUES)
    # The question carries the technique, model, configuration digest and the receipts.
    request = engine.sent[0][1]["request"]
    assert request["question"]["kind"] == "serving_technique_activation"
    assert {p["name"] for p in request["params"]} == {
        "technique",
        "model",
        "config_digest",
        "receipts",
    }
    # A regressed receipt set refuses and commits nothing.
    regressed, engine = served(
        monkeypatch,
        decide=EngineResponseError("INVALID_REQUEST", "TECHNIQUE_REGRESSED: too slow"),
    )
    with pytest.raises(OperationRefused) as refused:
        await serving_techniques.handle_technique_activate(
            regressed, _params("paged_cache"), _OP
        )
    assert refused.value.code == "POLICY_DENIED"
    assert refused.value.details["engine_code"] == "TECHNIQUE_REGRESSED"
    assert _committed(engine) == 0


@pytest.mark.spec("ARBORA-OPS-R054.1")
async def test_missing_mismatched_or_regressing_evidence_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for code in (
        "TECHNIQUE_EVIDENCE_MISSING",
        "TECHNIQUE_EVIDENCE_MISMATCH",
        "TECHNIQUE_REGRESSED",
    ):
        ctx, engine = served(
            monkeypatch, decide=EngineResponseError("INVALID_REQUEST", f"{code}: x")
        )
        with pytest.raises(OperationRefused) as refused:
            await serving_techniques.handle_technique_activate(
                ctx, _params("prefix_reuse"), _OP
            )
        assert refused.value.details["engine_code"] == code
        assert _committed(engine) == 0
    # A hold verdict is a decision to keep the current setup: nothing is committed.
    ctx, engine = served(monkeypatch, decide=lambda _p: answered("hold"))
    held = await serving_techniques.handle_technique_activate(
        ctx, _params("paged_cache"), _OP
    )
    assert held["value"]["verdict"] == "hold" and held["value"]["record_digest"] is None
    assert _committed(engine) == 0


@pytest.mark.spec("ARBORA-OPS-R054.1")
def test_the_technique_is_a_closed_set() -> None:
    from pydantic import ValidationError

    from arbora.api.ops.serving_techniques import TechniqueActivationRequest

    with pytest.raises(ValidationError):
        TechniqueActivationRequest(
            technique="turbo", model_ref="m", configuration_digest=DIGEST
        )
