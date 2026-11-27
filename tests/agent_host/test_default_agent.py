"""The default fleet agent is a graph record, so its skills are gated (ARBORA-HOST-R008.1001)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.agent_host.default_agent import (
    DefaultAgentDefinition,
    DefaultAgentRegistrar,
)
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import agent_library, agents
from arbora.core.agent_names import FLEET_AGENT_NAME
from arbora.pin_lag import medulla as pin_lag
from tests.a2a.test_operation_bridge import session
from tests.api._engine_double import EngineDouble
from tests.api.test_agent_library_ops import FakeLibrary, _context

_RUN = {op.id: op for op in agents.operations()}["agents.run"]
_SKILL = {"component_id": "skill:one", "definition_digest": "d3", "kind": "skill"}


class _A2A:
    def __init__(self) -> None:
        self.started: list[Any] = []

    async def send_message(self, **kwargs: Any) -> Any:
        self.started.append(kwargs)
        return SimpleNamespace(model_dump=lambda **_: {"id": "task:1"})


def _library(monkeypatch: pytest.MonkeyPatch) -> FakeLibrary:
    fake = FakeLibrary()
    monkeypatch.setattr(agent_library, "engine_send", fake.send)
    monkeypatch.setattr(agent_library, "_list_op_served", lambda: True)
    return fake


@pytest.mark.spec("ARBORA-HOST-R008.1001")
async def test_the_registrar_creates_the_entry_once_with_the_skill_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = _library(monkeypatch)
    registrar = DefaultAgentRegistrar(
        DefaultAgentDefinition(agent_id=FLEET_AGENT_NAME, skills=(_SKILL,))
    )
    context = _context()
    assert await registrar.ensure(context) == "created"
    assert await registrar.ensure(context) == "unchanged"
    head = library.heads[FLEET_AGENT_NAME]
    assert head["tenant_id"] == "tenant:one"
    assert [s["component_id"] for s in head["skills"]] == ["skill:one"]
    publishes = [op for _m, op, _i in library.sent if op["operation"] == "publish"]
    assert len(publishes) == 1


@pytest.mark.spec("ARBORA-HOST-R008.1001")
async def test_an_operator_entry_is_never_rewritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = _library(monkeypatch)
    library.heads[FLEET_AGENT_NAME] = {
        "agent_id": FLEET_AGENT_NAME,
        "entry_revision": 4,
        "skills": [],
    }
    registrar = DefaultAgentRegistrar(
        DefaultAgentDefinition(agent_id=FLEET_AGENT_NAME, skills=(_SKILL,))
    )
    assert await registrar.ensure(_context()) == "unchanged"
    assert library.heads[FLEET_AGENT_NAME]["skills"] == []


def _run_context(registrar: Any | None) -> Any:
    ctx = _context()
    ctx.caller.session = session(frozenset())
    ctx.caller.effective_scopes = frozenset()
    ctx.services = {"a2a": _A2A()}
    if registrar is not None:
        ctx.services["default_agent"] = registrar
    return ctx


@pytest.mark.spec("ARBORA-HOST-R008.1004")
async def test_a_default_run_without_a_library_entry_refuses_the_source_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _library(monkeypatch)
    ctx = _run_context(None)
    with pytest.raises(OperationRefused) as refused:
        await agents.handle_agent(ctx, {"message": {"parts": []}}, _RUN)
    assert refused.value.code == "POLICY_DENIED"
    assert refused.value.details["reason"] == "attestation_source_unavailable"
    assert ctx.services["a2a"].started == []


@pytest.mark.spec("ARBORA-HOST-R008.1001")
async def test_a_default_run_is_gated_on_the_skills_the_host_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = _library(monkeypatch)
    engine = EngineDouble(monkeypatch)
    engine.serve(
        "DecisionEval",
        lambda params: None,
        "LocalCertificationAttestation",
        "DecisionEvalOpCertification",
    )
    engine.install(pin_lag)
    registrar = DefaultAgentRegistrar(
        DefaultAgentDefinition(agent_id=FLEET_AGENT_NAME, skills=(_SKILL,))
    )
    ctx = _run_context(registrar)
    with pytest.raises(OperationRefused) as refused:
        await agents.handle_agent(ctx, {"message": {"parts": []}}, _RUN)
    # The registered skill has no declared evidence, so the gate refuses it.
    assert refused.value.details["reason"] == "attestation_missing"
    assert FLEET_AGENT_NAME in library.heads
    assert ctx.services["a2a"].started == []
