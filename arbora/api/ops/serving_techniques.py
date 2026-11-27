"""Technique-bound benchmark adoption (ARBORA-OPS-R054).

``serving.technique.activate`` asks the engine whether the benchmark receipts the
harness emitted support activating one serving technique (``Decide``
``serving_technique_activation``) and commits the sealed record only when the
engine's verdict is ``activate``. Arbora invents no threshold: the engine knobs
``serving.max_regression`` and ``serving.min_gain`` decide, and an engine refusal
(missing, mismatched or regressed evidence) leaves no serving effect.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.plan import params_digest
from arbora.api.ops import _common
from arbora.api.ops._engine import engine_send
from arbora.api.ops._question import (
    FamilyRefusal,
    chosen_option,
    decide_question,
    question_request,
)
from arbora.api.registry import Confirm, OpSpec

SCOPE = "agent:decision-write"
QUESTION = "serving_technique_activation"
Technique = Literal[
    "paged_cache", "prefix_reuse", "fused_kernels", "quantized_cache", "expert_dispatch"
]
#: The engine family's own refusals (MEDULLA-DECISION-ENGINE-R819).
ENGINE_REFUSALS = frozenset(
    {"TECHNIQUE_EVIDENCE_MISSING", "TECHNIQUE_EVIDENCE_MISMATCH", "TECHNIQUE_REGRESSED"}
)
_DIGEST = r"^[0-9a-f]{64}$"


class TechniqueActivationRequest(_common.Params):
    technique: Technique
    model_ref: str = Field(min_length=1, max_length=256)
    configuration_digest: str = Field(pattern=_DIGEST)
    #: The harness benchmark receipts; the engine checks each binds the same subject.
    receipts: list[dict[str, Any]] = Field(default_factory=list, max_length=64)


class TechniqueActivation(_common.FrozenState):
    technique: Technique
    model_ref: str
    configuration_digest: str
    receipt_digests: tuple[str, ...]
    verdict: Literal["activate", "hold"]
    record_digest: str | None = None


class TechniqueActivationResult(_common.Params):
    value: dict[str, Any]


def _receipt_digest(receipt: Mapping[str, Any]) -> str:
    return params_digest(dict(receipt))


async def _commit(context: Any, record: Mapping[str, Any]) -> None:
    """The one commit path of ``decide.commit``: the engine ``DecisionCommit`` method."""
    await engine_send(
        context,
        "DecisionCommit",
        {"request": {"record": dict(record)}},
        idempotent=True,
    )


async def handle_technique_activate(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Decide with the engine and commit the sealed record on ``activate`` only."""
    from medulla import decision_client as dc

    from arbora.api.ops.plans import _feature_schema

    receipts = list(params.get("receipts") or ())
    request = question_request(
        str(context.caller.tenant),
        f"serving.technique:{params['technique']}",
        QUESTION,
        dc.declared([dc.DeclaredOption("activation")]),
        await _feature_schema(context, str(context.caller.tenant)),
        [
            dc.param("technique", "text", params["technique"]),
            dc.param("model", "text", params["model_ref"]),
            dc.param("config_digest", "text", params["configuration_digest"]),
            dc.param("receipts", "text", json.dumps(receipts, sort_keys=True)),
        ],
    )
    try:
        record = await decide_question(
            context, QUESTION, request, family_codes=ENGINE_REFUSALS
        )
    except FamilyRefusal as refused:
        # The engine named the family refusal; nothing is committed or applied.
        raise OperationRefused(
            "POLICY_DENIED", {"engine_code": refused.code}
        ) from refused
    verdict = chosen_option(record)
    if verdict not in ("activate", "hold"):
        raise OperationRefused("RECEIPT_MISMATCH", {"reason": "unknown verdict"})
    digest: str | None = None
    if verdict == "activate":
        await _commit(context, record)
        digest = params_digest(dict(record))
    activation = TechniqueActivation(
        technique=params["technique"],
        model_ref=params["model_ref"],
        configuration_digest=params["configuration_digest"],
        receipt_digests=tuple(_receipt_digest(r) for r in receipts),
        verdict=verdict,
        record_digest=digest,
    )
    return {"value": activation.model_dump(mode="json")}


def operations() -> tuple[OpSpec, ...]:
    return (
        _common.build_write_op(
            op_id="serving.technique.activate",
            summary="Activate a serving technique the benchmark receipts support",
            examples=("activate this serving technique",),
            params=TechniqueActivationRequest,
            result=TechniqueActivationResult,
            handler="arbora.api.ops.serving_techniques.handle_technique_activate",
            scope=SCOPE,
            confirm=Confirm.PLAN,
        ),
    )


#: The commit is the engine's own write, replayed by the idempotency key.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "serving.technique.activate": "medulla:DecisionCommit"
}

specs = operations

__all__ = [
    "ENGINE_REFUSALS",
    "NATIVE_IDEMPOTENCY",
    "TechniqueActivation",
    "TechniqueActivationRequest",
    "handle_technique_activate",
    "operations",
    "specs",
]
