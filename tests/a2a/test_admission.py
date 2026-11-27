"""Tool-subset admission from the engine's committed harness decision (ARBORA-A2A-R016 to R020).

The decider is scripted: one answer per call, every call recorded, nothing sleeps. The
engine adapter is exercised against a scripted ``Decide`` that echoes the request.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from types import SimpleNamespace
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from arbora.a2a.admission import (
    HarnessConfigMirror,
    ToolSubsetAdmissionDecision,
    ToolSubsetAdmissionRefusalReason,
    ToolSubsetAdmissionRequest,
    admit_tool_subset,
    priority_for,
)
from arbora.a2a.engine_decider import EngineHarnessConfigDecider
from arbora.api.domain.a2a.models import A2AContextBudget
from tests.a2a._admission_support import (
    DIGEST,
    TOOLS,
    Scripted,
    answer_json,
    part,
    record,
    request,
)


def _admit(req: Any, decider: Any, **kw: Any) -> ToolSubsetAdmissionDecision:
    return asyncio.run(admit_tool_subset(req, decider, **kw))


@pytest.mark.spec("ARBORA-A2A-R016.2")
def test_request_requires_model_profile_and_task_class() -> None:
    base = {
        "agent_graph_ref": "g",
        "context_budget": A2AContextBudget(tokens=512),
        "model_profile_ref": "p",
        "task_class": "t",
    }
    ToolSubsetAdmissionRequest(**base)
    for missing in ("model_profile_ref", "task_class"):
        with pytest.raises(ValidationError):
            ToolSubsetAdmissionRequest(
                **{k: v for k, v in base.items() if k != missing}
            )
        with pytest.raises(ValidationError):
            ToolSubsetAdmissionRequest(**{**base, missing: ""})
        with pytest.raises(ValidationError):
            ToolSubsetAdmissionRequest(**{**base, missing: "x" * 513})


@pytest.mark.spec("ARBORA-A2A-R016.3")
def test_admitted_decision_carries_harness_config_and_budget() -> None:
    admitted = _admit(request(4_096), Scripted())
    assert admitted.admitted and admitted.admitted_tools == tuple(TOOLS)
    assert admitted.harness_config is not None
    assert admitted.decision_record_ref == "record:harness:1"
    assert admitted.context_budget_tokens == 4_096
    refused = _admit(request(None), Scripted())
    assert (refused.harness_config, refused.decision_record_ref) == (None, None)
    assert refused.context_budget_tokens is None
    # The decision type refuses a partly carried answer.
    with pytest.raises(ValidationError):
        ToolSubsetAdmissionDecision(admitted=True, provenance="p")
    with pytest.raises(ValidationError):
        ToolSubsetAdmissionDecision(
            admitted=False,
            refusal_reason="CONTEXT_BUDGET_MISSING",
            provenance="p",
            decision_record_ref="r",
        )


@pytest.mark.spec("ARBORA-A2A-R016.3")
def test_mirror_round_trips_engine_vectors() -> None:
    from medulla.generated.models import (
        HarnessConfigAnswer,
        HarnessPart_ActionSpaceDecided,
    )

    vector = answer_json(
        soft_threshold=part(
            conservative_default={"numerator": 3, "denominator": 4},
            missing_fact=["tokens.p95"],
        )
    )
    # The engine tags each part ``decision`` on main and ``status`` in the pinned wheel.
    tag = next(
        k
        for k in HarnessPart_ActionSpaceDecided.model_fields
        if k in {"status", "decision"}
    )
    engine = HarnessConfigAnswer.model_validate(
        {
            name: {
                tag if key == "decision" else key: value for key, value in body.items()
            }
            for name, body in vector.items()
        }
    )
    mirror = HarnessConfigMirror.model_validate(engine.model_dump(mode="json"))
    assert mirror.soft_threshold.decision == "abstained"
    assert mirror.soft_threshold.missing_fact == ("tokens.p95",)
    assert mirror.tool_subset.value == TOOLS
    again = mirror.model_dump(mode="json", by_alias=False, exclude_none=True)
    assert set(again) == set(engine.model_dump(mode="json"))
    with pytest.raises(ValidationError):
        HarnessConfigMirror.model_validate({**answer_json(), "extra": part(1)})


@pytest.mark.spec("ARBORA-A2A-R016.4")
def test_refusal_reasons_are_closed_set() -> None:
    assert set(get_args(ToolSubsetAdmissionRefusalReason)) == {
        "HARNESS_CONFIG_UNAVAILABLE",
        "HARNESS_CONFIG_ABSTAINED",
        "ENVELOPE_LACKS_ALLOWED_TOOL_SUBSET",
        "ENVELOPE_LACKS_HARNESS_CONFIG",
        "CONTEXT_BUDGET_MISSING",
    }
    assert "EG_ASSEMBLE_UNAVAILABLE" not in get_args(ToolSubsetAdmissionRefusalReason)


@pytest.mark.spec("ARBORA-A2A-R016.1")
def test_unreachable_engine_refuses_closed_with_provenance() -> None:
    refused = _admit(request(), Scripted(ConnectionError("down")))
    assert refused.admitted is False and refused.admitted_tools == ()
    assert refused.refusal_reason == "HARNESS_CONFIG_UNAVAILABLE"
    assert refused.provenance == "eg.decide:harness_config:unavailable"


@pytest.mark.spec("ARBORA-A2A-R017.1")
def test_decider_port_returns_typed_answer() -> None:
    sent: list[dict[str, Any]] = []

    async def decide(body: dict[str, Any]) -> Any:
        sent.append(body)
        principal = "principal:sha256:" + hashlib.sha256(b"person:one").hexdigest()
        return {
            "records": [
                {
                    "record_id": "record:harness:9",
                    "record_digest": "sha256:" + DIGEST,
                    "caller_principal": principal,
                    "tenant_id": body["tenant_id"],
                    "question": body["question"],
                    "inputs": {"params": body["params"]},
                    # ``StatisticalDecisionRecord.answer``: ``FamilyAnswer::HarnessConfig``
                    # whose value is the ``HarnessConfigEvaluationEvidence``.
                    "answer": {
                        "type": "harness_config",
                        "value": {"answer": answer_json()},
                    },
                }
            ]
        }

    decider = EngineHarnessConfigDecider(
        decide,
        feature_schema=lambda tenant: {"features": []},
        principal=lambda: "person:one",
    )
    got = asyncio.run(
        decider.decide(
            tenant="tenant:one",
            model_profile_ref="profile:one",
            task_class="task:chat",
            budget_tokens=4_096,
            priority="interactive",
        )
    )
    assert got.decision_record_ref == "record:harness:9" and got.digest == DIGEST
    assert got.expires_at_ms is None and got.answer.tool_subset.value == TOOLS
    [body] = sent
    assert body["question"]["kind"] == "harness_config"
    assert {p["name"] for p in body["params"]} == {
        "model_profile_ref",
        "task_class",
        "usable_context_budget_tokens",
        "priority",
    }
    assert (
        EngineHarnessConfigDecider(
            decide,
            feature_schema=lambda tenant: {},
            principal=lambda: "x",
            timeout_s=900,
        )._timeout_s
        == 60
    )


@pytest.mark.spec("ARBORA-A2A-R017.2")
def test_admission_makes_one_engine_call() -> None:
    decider = Scripted()
    assert asyncio.run(admit_tool_subset(request(), decider)).admitted
    assert len(decider.calls) == 1
    assert decider.calls[0] == {
        "tenant": "tenant:one",
        "model_profile_ref": "profile:one",
        "task_class": "task:chat",
        "budget_tokens": 4_096,
        "priority": "interactive",
        "tool_refs": (),
    }
    assert inspect.iscoroutinefunction(admit_tool_subset)


@pytest.mark.spec("ARBORA-A2A-R017.3")
def test_priority_is_set_from_principal_class() -> None:
    assert priority_for(SimpleNamespace(principal_kind="human")) == "interactive"
    assert priority_for(SimpleNamespace(principal_kind="service")) == "orchestration"
    for kind in ("agent", "anonymous", ""):
        with pytest.raises(PermissionError):
            priority_for(SimpleNamespace(principal_kind=kind))
    # A caller-supplied ``priority`` key reaches nothing: the request has no such input.
    caller = SimpleNamespace(
        principal_kind="human", metadata={"priority": "orchestration"}
    )
    assert priority_for(caller) == "interactive"


@pytest.mark.spec("ARBORA-A2A-R017.4")
def test_missing_budget_refuses_before_engine_call() -> None:
    decider = Scripted()
    refused = _admit(request(None), decider)
    assert refused.refusal_reason == "CONTEXT_BUDGET_MISSING"
    assert decider.calls == []


@pytest.mark.spec("ARBORA-A2A-R018.2")
def test_envelope_lacking_harness_config_refuses() -> None:
    class WithoutDigest:
        model_fields = {"allowed_tool_subset": 1, "decision_record_ref": 1}

    class WithoutSubset:
        model_fields = {"decision_record_ref": 1, "harness_config_digest": 1}

    decider = Scripted()
    refused = _admit(request(), decider, dispatch_model=WithoutDigest)
    assert refused.refusal_reason == "ENVELOPE_LACKS_HARNESS_CONFIG"
    assert len(decider.calls) == 1  # the engine answered; the envelope still refuses
    refused = _admit(request(), Scripted(), dispatch_model=WithoutSubset)
    assert refused.refusal_reason == "ENVELOPE_LACKS_ALLOWED_TOOL_SUBSET"
    assert _admit(
        request(), Scripted()
    ).admitted  # the shipped request model carries all


@pytest.mark.spec("ARBORA-A2A-R019")
def test_every_decision_has_provenance_and_typed_reason() -> None:
    cases = [
        _admit(request(), Scripted()),
        _admit(request(None), Scripted()),
        _admit(request(), Scripted(OSError("x"))),
        _admit(
            request(),
            Scripted(
                record(tool_subset=part(conservative_default=[], missing_fact=["f"]))
            ),
        ),
        _admit(request(), Scripted(record(expires_at_ms=1)), clock=lambda: 1.0),
    ]
    reasons = set(get_args(ToolSubsetAdmissionRefusalReason))
    for decision in cases:
        assert decision.provenance
        if decision.admitted:
            assert decision.refusal_reason is None
        else:
            assert decision.refusal_reason in reasons


@pytest.mark.spec("ARBORA-A2A-R019.1")
def test_provenance_names_harness_decision_record() -> None:
    assert (
        _admit(request(), Scripted()).provenance
        == "eg.decide:harness_config:record:harness:1"
    )
    abstained = _admit(
        request(),
        Scripted(record(tool_subset=part(conservative_default=[], missing_fact=["f"]))),
    )
    assert abstained.provenance.startswith("eg.decide:harness_config:abstained")


@pytest.mark.spec("ARBORA-A2A-R020.1")
def test_abstention_refuses_and_does_not_apply_default() -> None:
    answer = record(
        soft_threshold=part(
            conservative_default="one-half", missing_fact=["tokens.p95", "cost"]
        )
    )
    refused = _admit(request(), Scripted(answer))
    assert refused.refusal_reason == "HARNESS_CONFIG_ABSTAINED"
    assert refused.admitted_tools == () and refused.harness_config is None
    assert "soft_threshold:tokens.p95+cost=one-half" in refused.provenance


@pytest.mark.spec("ARBORA-A2A-R020.2")
def test_unavailable_stale_or_partial_harness_answer_refuses() -> None:
    unreachable = _admit(request(), Scripted(TimeoutError()))
    stale = _admit(request(), Scripted(record(expires_at_ms=2_000)), clock=lambda: 2.0)
    fresh = _admit(request(), Scripted(record(expires_at_ms=2_001)), clock=lambda: 2.0)
    partial = _admit(
        request(),
        Scripted(
            record(
                context_policy=part("elide_then_summarize"),
                soft_threshold=part(None),
            )
        ),
    )
    for refused in (unreachable, stale, partial):
        assert refused.refusal_reason == "HARNESS_CONFIG_UNAVAILABLE"
    assert stale.provenance.endswith(":stale") and partial.provenance.endswith(
        ":partial"
    )
    assert fresh.admitted


@pytest.mark.spec("ARBORA-A2A-R020")
def test_partial_or_stale_answer_refuses_never_trims() -> None:
    too_many = record(tool_subset=part([f"tool.{i}" for i in range(65)]))
    refused = _admit(request(), Scripted(too_many))
    assert refused.admitted is False and refused.admitted_tools == ()
    # No memory between requests: a refusal after an admission returns no earlier subset.
    decider = Scripted()
    assert _admit(request(), decider).admitted
    decider.answer = OSError("down")
    after = _admit(request(), decider)
    assert after.admitted is False and after.admitted_tools == ()


def _harness_decider(sent: list[dict[str, Any]], **evidence: Any) -> Any:
    async def decide(body: dict[str, Any]) -> Any:
        sent.append(body)
        principal = "principal:sha256:" + hashlib.sha256(b"person:one").hexdigest()
        return {
            "records": [
                {
                    "record_id": "record:harness:9",
                    "record_digest": "sha256:" + DIGEST,
                    "caller_principal": principal,
                    "tenant_id": body["tenant_id"],
                    "question": body["question"],
                    "inputs": {"params": body["params"]},
                    "answer": {
                        "type": "harness_config",
                        "value": {"answer": answer_json(), **evidence},
                    },
                }
            ]
        }

    return EngineHarnessConfigDecider(
        decide,
        feature_schema=lambda tenant: {"features": []},
        principal=lambda: "person:one",
    )


@pytest.mark.spec("ARBORA-A2A-R017.1")
def test_routed_tool_refs_are_declared_as_the_harness_candidates() -> None:
    sent: list[dict[str, Any]] = []
    asyncio.run(
        _harness_decider(sent).decide(
            tenant="tenant:one",
            model_profile_ref="profile:one",
            task_class="task:chat",
            budget_tokens=4_096,
            priority="interactive",
            tool_refs=("tool:search", "tool:read"),
        )
    )
    [body] = sent
    assert body["candidates"]["source"] == "declared"
    assert {o["option_id"] for o in body["candidates"]["options"]} == {
        "profile:one",
        "tool:search",
        "tool:read",
    }


@pytest.mark.spec("ARBORA-A2A-R020.2")
def test_the_expiry_sealed_on_the_evidence_is_read_and_enforced() -> None:
    sent: list[dict[str, Any]] = []
    fields = {
        "tenant": "tenant:one",
        "model_profile_ref": "profile:one",
        "task_class": "task:chat",
        "budget_tokens": 4_096,
        "priority": "interactive",
    }
    sealed = asyncio.run(_harness_decider(sent, expires_at_ms=5_000).decide(**fields))
    assert sealed.expires_at_ms == 5_000
    # The pinned engine seals none: nothing is read and no lifetime is invented.
    assert asyncio.run(_harness_decider(sent).decide(**fields)).expires_at_ms is None
    # A malformed expiry is a malformed record, not "never expires".
    for bad in (-1, "soon", True):
        with pytest.raises(ValueError):
            asyncio.run(_harness_decider(sent, expires_at_ms=bad).decide(**fields))


@pytest.mark.spec("ARBORA-A2A-R020.2")
def test_valid_for_ms_rides_the_request_only_when_a_host_asks_for_one() -> None:
    sent: list[dict[str, Any]] = []
    fields = {
        "tenant": "tenant:one",
        "model_profile_ref": "profile:one",
        "task_class": "task:chat",
        "budget_tokens": 4_096,
        "priority": "interactive",
    }
    asyncio.run(_harness_decider(sent).decide(**fields))
    asyncio.run(_harness_decider(sent).decide(**fields, valid_for_ms=60_000))
    plain, asked = ({p["name"]: p["value"] for p in body["params"]} for body in sent)
    assert "valid_for_ms" not in plain
    assert asked["valid_for_ms"] == {"type": "int", "value": 60_000}


@pytest.mark.spec("ARBORA-A2A-R020.2")
def test_admission_forwards_the_requested_lifetime_to_the_decider() -> None:
    seen: list[dict[str, Any]] = []

    class Recording:
        async def decide(self, **fields: Any) -> Any:
            seen.append(fields)
            raise RuntimeError("no engine here")

    for lifetime in (None, 30_000):
        asked = request().model_copy(update={"valid_for_ms": lifetime})
        asyncio.run(admit_tool_subset(asked, Recording()))  # type: ignore[arg-type]
    assert "valid_for_ms" not in seen[0]
    assert seen[1]["valid_for_ms"] == 30_000
