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

_MODELS = (
    "CertificationReadRequest",
    "CertificationReadAnswer",
    "DecisionEvalOpCertification",
)
_SKILL = {"id": "skill:one", "revision": 3, "digest": "d3"}
_RUN = {op.id: op for op in agents.operations()}["agents.run"]
_PLAN = {op.id: op for op in plans.operations()}["agents.plan_run"]


def _stored(*, skill: str = "skill:one", digest: str = "d3", revision: int = 3) -> Any:
    """The engine's ``CertificationReadAnswer`` holding the latest attestation."""
    return {
        "attestation": {
            "body": {
                "skill": {"component_id": skill, "definition_digest": digest},
                "completion_revision": revision,
            },
            "digest": "a1",
        }
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


@pytest.fixture(autouse=True)
def _default_agent_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host registers the default fleet agent: its library entry holds no skill."""
    from arbora.api.ops import agent_library

    registered = EngineDouble(monkeypatch)
    entry = {"agent_id": "arbora", "skills": []}
    registered.serve("AgentLibrary", lambda params: {"entry": entry})
    registered.install(agent_library)


_REFUSALS = (
    ({"attestation": None}, "attestation_missing"),
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
        "request": {
            "tenant_id": "tenant:one",
            "component_id": "skill:one",
            "definition_digest": "d3",
        },
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

    monkeypatch.setattr(models, "CertificationReadRequest", object, raising=False)
    monkeypatch.setattr(models, "CertificationReadAnswer", object, raising=False)
    monkeypatch.delattr(models, "DecisionEvalOpCertification", raising=False)
    ctx = _run_context(_A2A())
    with pytest.raises(OperationRefused) as refused:
        await agents.handle_agent(
            ctx, {"message": {"parts": []}, "skills": [_SKILL]}, _RUN
        )
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "DecisionEvalOpCertification"


@pytest.mark.spec("ARBORA-OPS-R872")
async def test_a_default_agent_run_declaring_no_skill_has_nothing_to_attest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch, None)
    from arbora.api.ops import agent_library

    library = EngineDouble(monkeypatch)
    entry = {"agent_id": "arbora", "skills": []}
    library.serve("AgentLibrary", lambda params: {"entry": entry})
    library.install(agent_library)
    a2a = _A2A()
    await agents.handle_agent(_run_context(a2a), {"message": {"parts": []}}, _RUN)
    # The default agent's own library entry holds no skill, so no attestation is read.
    assert engine.sent == [] and len(a2a.started) == 1
    assert library.sent[0][1]["op"]["agent_id"] == "arbora"


@pytest.mark.spec("ARBORA-OPS-R872")
def test_both_run_operations_declare_the_skills_parameter() -> None:
    assert "skills" in agents.AgentRunParams.model_fields
    assert "skills" in plans.PlanRunParams.model_fields
    assert SimpleNamespace  # the structural doubles above stand in for the engine


def _library(monkeypatch: pytest.MonkeyPatch, skills: Any) -> EngineDouble:
    """A structural engine behind the ``AgentLibrary`` head read of the named agent."""
    from arbora.api.ops import agent_library

    library = EngineDouble(monkeypatch)
    entry = {"agent_id": "helper", "skills": skills}
    library.serve("AgentLibrary", lambda params: {"entry": entry})
    library.install(agent_library)
    return library


_GRAPH_SKILL = [
    {"component_id": "skill:one", "definition_digest": "d3", "kind": "skill"}
]
_NAMED = {"message": {"parts": [], "metadata": {"agent_name": "helper"}}}


@pytest.mark.spec("ARBORA-OPS-R872")
async def test_a_named_agent_declaring_no_skill_cannot_skip_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine(monkeypatch, _stored())
    library = _library(monkeypatch, _GRAPH_SKILL)
    a2a = _A2A()
    with pytest.raises(OperationRefused) as refused:
        await agents.handle_agent(_run_context(a2a), dict(_NAMED), _RUN)
    # The skills come from the agent's definition node, read as the verified caller.
    assert refused.value.details["reason"] == "attestation_missing"
    assert a2a.started == []
    [(method, sent, _idem)] = library.sent
    assert method == "AgentLibrary"
    assert sent["op"] == {
        "operation": "current",
        "agent_id": "helper",
        "tenant_id": "tenant:one",
    }


@pytest.mark.spec("ARBORA-OPS-R872")
async def test_a_declared_skill_at_another_digest_than_the_graph_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine(monkeypatch, _stored())
    _library(monkeypatch, _GRAPH_SKILL)
    a2a = _A2A()
    params = {**_NAMED, "skills": [{**_SKILL, "digest": "dX"}]}
    with pytest.raises(OperationRefused) as refused:
        await agents.handle_agent(_run_context(a2a), params, _RUN)
    assert refused.value.details["reason"] == "attestation_mismatch"
    assert a2a.started == []


@pytest.mark.spec("ARBORA-OPS-R872")
async def test_the_graph_skills_run_once_attested_and_declared_skills_only_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine(monkeypatch, _stored())
    _library(monkeypatch, _GRAPH_SKILL)
    a2a = _A2A()
    params = {**_NAMED, "skills": [_SKILL]}
    await agents.handle_agent(_run_context(a2a), params, _RUN)
    assert len(a2a.started) == 1
    # An extra declared skill is gated too: it never replaces the graph set.
    extra = {
        "id": "skill:two",
        "revision": 1,
        "digest": "e",
    }
    a2a = _A2A()
    with pytest.raises(OperationRefused) as refused:
        await agents.handle_agent(
            _run_context(a2a), {**_NAMED, "skills": [_SKILL, extra]}, _RUN
        )
    assert refused.value.details["reason"] == "attestation_mismatch"
    assert a2a.started == []


@pytest.mark.spec("ARBORA-OPS-R872")
@pytest.mark.parametrize("entry", [None, {"agent_id": "helper"}, {"skills": "x"}])
async def test_an_unreadable_definition_refuses_the_source_unavailable(
    monkeypatch: pytest.MonkeyPatch, entry: Any
) -> None:
    from arbora.api.ops import agent_library

    _engine(monkeypatch, _stored())
    library = EngineDouble(monkeypatch)
    library.serve("AgentLibrary", lambda params: {"entry": entry})
    library.install(agent_library)
    a2a = _A2A()
    with pytest.raises(OperationRefused) as refused:
        await agents.handle_agent(
            _run_context(a2a), {**_NAMED, "skills": [_SKILL]}, _RUN
        )
    assert refused.value.code == "POLICY_DENIED"
    assert refused.value.details["reason"] == "attestation_source_unavailable"
    assert a2a.started == []


@pytest.mark.spec("ARBORA-OPS-R872")
async def test_a_plan_task_assignee_definition_is_gated_with_no_declared_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine(monkeypatch, _stored())
    _library(monkeypatch, _GRAPH_SKILL)
    ctx = context()
    ctx.caller.session = session(frozenset())
    plan = {"tasks": [{"task_id": "a", "prompt": "p", "assignee": "helper"}]}
    with use_session(ctx.caller.session), pytest.raises(OperationRefused) as refused:
        await plans.handle_plan_run(ctx, {"plan": plan}, _PLAN)
    assert refused.value.details["reason"] == "attestation_missing"


@pytest.mark.spec("ARBORA-OPS-R872")
async def test_a_composed_definition_reader_replaces_the_engine_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch, _stored())
    asked: list[str] = []

    async def reader(agent: str) -> Any:
        asked.append(agent)
        return {"skills": _GRAPH_SKILL}

    a2a = _A2A()
    ctx = _run_context(a2a)
    ctx.services["attestation_definition_reader"] = reader
    await agents.handle_agent(ctx, {**_NAMED, "skills": [_SKILL]}, _RUN)
    assert asked == ["helper"] and len(a2a.started) == 1
    assert [m for m, *_ in engine.sent] == ["DecisionEval"]


@pytest.mark.spec("ARBORA-OPS-R872")
async def test_a_named_workflow_takes_its_skills_from_the_registry_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.invoke import certification_gate as gate

    seen: list[dict[str, Any]] = []

    async def send(context: Any, method: str, params: dict[str, Any]) -> Any:
        seen.append({"method": method, **params})
        return {"skills": _GRAPH_SKILL}

    monkeypatch.setattr("arbora.api.ops._engine.engine_send", send)
    identity = {"name": "wf", "version": "1"}
    skills = await gate.definition_skills(context(), workflow=identity)
    assert skills == [{"id": "skill:one", "digest": "d3"}]
    assert seen == [{"method": "WorkflowRegistry", "op": "read", "identity": identity}]

    # A record without a skills list is an unreadable definition, never an empty one.
    async def bare(context: Any, method: str, params: dict[str, Any]) -> Any:
        return {"identity": identity}

    monkeypatch.setattr("arbora.api.ops._engine.engine_send", bare)
    with pytest.raises(OperationRefused) as refused:
        await gate.definition_skills(context(), workflow=identity)
    assert refused.value.details["reason"] == "attestation_source_unavailable"
