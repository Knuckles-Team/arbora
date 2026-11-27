"""``agents.run`` and ``agents.plan_run`` need each declared skill's attestation (ARBORA-OPS-R872).

The gate runs inside the real handlers before any run effect. The engine behind the
``DecisionEval`` ``certification`` read is a structural double of an engine release that
declares the op (the pinned release does not yet).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import agents, plans
from arbora.pin_lag import medulla as pin_lag
from arbora.security.session import use_session
from tests.a2a.test_operation_bridge import session
from tests.api._engine_double import EngineDouble, context

_MODELS = ("LocalCertificationAttestation", "DecisionEvalOpCertification")
_SKILL = {"id": "skill:one", "revision": 3, "digest": "d3", "attestation_digest": "a1"}
_RUN = {op.id: op for op in agents.operations()}["agents.run"]
_PLAN = {op.id: op for op in plans.operations()}["agents.plan_run"]


def _stored(*, skill: str = "skill:one", digest: str = "d3", revision: int = 3) -> Any:
    return {
        "body": {
            "skill": {"component_id": skill, "definition_digest": digest},
            "completion_revision": revision,
        },
        "digest": "a1",
    }


class _A2A:
    """The A2A authority: records each run that reaches it."""

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> Any:
        self.started.append(kwargs)
        return SimpleNamespace(model_dump=lambda **_: {"id": "task:1"})


def _engine(monkeypatch: pytest.MonkeyPatch, stored: Any) -> EngineDouble:
    engine = EngineDouble(monkeypatch)
    engine.serve("DecisionEval", lambda params: stored, *_MODELS)
    engine.install(pin_lag)
    return engine


def _run_context(a2a: _A2A) -> Any:
    ctx = context()
    ctx.caller.session = session(frozenset())
    ctx.services = {"a2a": a2a}
    return ctx


_REFUSALS = (
    (None, "attestation_missing"),
    (_stored(revision=2), "attestation_stale"),
    (_stored(digest="dX"), "attestation_mismatch"),
    (_stored(skill="skill:other"), "attestation_mismatch"),
)


@pytest.mark.spec("ARBORA-OPS-R872")
@pytest.mark.parametrize(("stored", "reason"), _REFUSALS)
async def test_agents_run_refuses_each_attestation_case_before_any_run(
    monkeypatch: pytest.MonkeyPatch, stored: Any, reason: str
) -> None:
    engine = _engine(monkeypatch, stored)
    a2a = _A2A()
    params = {"message": {"parts": []}, "skills": [_SKILL]}
    with pytest.raises(OperationRefused) as refused:
        await agents.handle_agent(_run_context(a2a), params, _RUN)
    assert refused.value.code == "POLICY_DENIED"
    assert refused.value.details["reason"] == reason
    assert a2a.started == []
    [(method, sent, _idem)] = engine.sent
    assert method == "DecisionEval"
    assert sent["op"] == {
        "op": "certification",
        "request": {"tenant_id": "tenant:one", "digest": "a1"},
    }


@pytest.mark.spec("ARBORA-OPS-R872")
async def test_agents_run_starts_once_the_attestation_is_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine(monkeypatch, _stored())
    a2a = _A2A()
    params = {"message": {"parts": []}, "skills": [_SKILL]}
    result = await agents.handle_agent(_run_context(a2a), params, _RUN)
    assert len(a2a.started) == 1
    assert result["value"] == {"id": "task:1"}


@pytest.mark.spec("ARBORA-OPS-R872")
@pytest.mark.parametrize(("stored", "reason"), _REFUSALS)
async def test_agents_plan_run_refuses_before_the_plan_is_decided(
    monkeypatch: pytest.MonkeyPatch, stored: Any, reason: str
) -> None:
    _engine(monkeypatch, stored)
    ctx = context()
    ctx.caller.session = session(frozenset())
    params = {"plan": {"tasks": []}, "skills": [_SKILL]}
    with use_session(ctx.caller.session), pytest.raises(OperationRefused) as refused:
        await plans.handle_plan_run(ctx, params, _PLAN)
    assert refused.value.code == "POLICY_DENIED"
    assert refused.value.details["reason"] == reason


@pytest.mark.spec("ARBORA-OPS-R872")
async def test_a_pin_without_the_certification_read_refuses_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medulla.generated import models

    monkeypatch.setattr(models, "LocalCertificationAttestation", object, raising=False)
    monkeypatch.delattr(models, "DecisionEvalOpCertification", raising=False)
    ctx = _run_context(_A2A())
    with pytest.raises(OperationRefused) as refused:
        await agents.handle_agent(
            ctx, {"message": {"parts": []}, "skills": [_SKILL]}, _RUN
        )
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "DecisionEvalOpCertification"


@pytest.mark.spec("ARBORA-OPS-R872")
async def test_a_run_that_declares_no_skill_has_nothing_to_attest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch, None)
    a2a = _A2A()
    await agents.handle_agent(_run_context(a2a), {"message": {"parts": []}}, _RUN)
    assert engine.sent == [] and len(a2a.started) == 1


@pytest.mark.spec("ARBORA-OPS-R872")
def test_both_run_operations_declare_the_skills_parameter() -> None:
    assert "skills" in agents.AgentRunParams.model_fields
    assert "skills" in plans.PlanRunParams.model_fields
    assert SimpleNamespace  # the structural doubles above stand in for the engine
