"""One role template serves two tenants over isolated business profiles (ARBORA-OPS-R917.4)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api import model_route
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import agent_library, agents
from arbora.api.ops import operator_surface as surface
from tests.api._engine_double import EngineDouble, context

pytestmark = pytest.mark.spec("ARBORA-OPS-R917.4")


class _A2A:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send_message(self, *, message: dict[str, Any], **_: Any) -> Any:
        self.messages.append(message)
        return SimpleNamespace(model_dump=lambda **_kw: {"id": "task"})


def _run_context(tenant: str, a2a: _A2A) -> Any:
    base = context(tenant)
    base.caller.session = object()
    base.services = {"a2a": a2a}
    return base


async def test_one_template_two_tenants_isolated_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = {
        "tenant:one": ({"entity": "one"}, 7),
        "tenant:two": ({"entity": "two"}, 9),
    }
    instances = {("tenant:one", "role:one"), ("tenant:two", "role:two")}
    profile_reads: list[str] = []
    engine = EngineDouble(monkeypatch)

    def _kv_get(p: dict[str, Any]) -> Any:
        if engine.tenant not in profiles or p["namespace"] != "business.profile":
            return None
        profile_reads.append(engine.tenant)
        profile, version = profiles[engine.tenant]
        return {"value": json.dumps(profile).encode(), "version": version}

    def _library(p: dict[str, Any]) -> dict[str, Any]:
        if (engine.tenant, p["op"]["agent_id"]) not in instances:
            return {"entry": None}
        return {
            "entry": {"agent_id": p["op"]["agent_id"], "tools": [{"component_id": "t"}]}
        }

    engine.serve("KvGet", _kv_get)
    engine.serve("AgentLibrary", _library)
    engine.install(surface)
    engine.install(agent_library)
    engine.install(model_route)
    monkeypatch.setattr("arbora.security.session.use_session", _noop_session)
    a2a = _A2A()
    run = next(op for op in agents.operations() if op.id == "agents.run")

    def _message(instance: str) -> dict[str, Any]:
        return {"kind": "message", "metadata": {"role_instance_id": instance}}

    for tenant, instance in (("tenant:one", "role:one"), ("tenant:two", "role:two")):
        await agents.handle_agent(
            _run_context(tenant, a2a), {"message": _message(instance)}, run
        )
    # Each run reads only its own tenant's profile and carries that profile's reference.
    assert profile_reads == ["tenant:one", "tenant:two"]
    assert [m["metadata"]["profile_ref"][-2:] for m in a2a.messages] == ["@7", "@9"]
    # Neither tenant sees the other's instance: a foreign one answers as an absent one.
    for tenant, foreign in (("tenant:one", "role:two"), ("tenant:two", "role:one")):
        with pytest.raises(OperationRefused) as hidden:
            await agents.handle_agent(
                _run_context(tenant, a2a), {"message": _message(foreign)}, run
            )
        assert hidden.value.code == "NOT_FOUND"
    assert len(a2a.messages) == 2
    message = _message("role:one")
    # A tenant with no profile refuses before a run is admitted.
    with pytest.raises(OperationRefused) as unset:
        await agents.handle_agent(
            _run_context("tenant:three", a2a), {"message": message}, run
        )
    assert unset.value.code == "PROFILE_UNSET" and len(a2a.messages) == 2
    # A run that names no role instance reads no profile.
    plain = {"kind": "message"}
    before = len(engine.sent)
    await agents.handle_agent(
        _run_context("tenant:three", a2a), {"message": plain}, run
    )
    assert len(engine.sent) == before


def _noop_session(_session: Any) -> Any:
    from contextlib import nullcontext

    return nullcontext()


@pytest.mark.spec("ARBORA-OPS-R919.3")
async def test_engine_operations_are_allowlisted_and_counted_in_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = EngineDouble(monkeypatch)
    engine.serve(
        "KvGet",
        lambda p: (
            {"value": json.dumps({"entity": "e"}).encode(), "version": 1}
            if p["namespace"] == "business.profile"
            else None
        ),
    )
    engine.serve(
        "AgentLibrary",
        lambda p: {
            "entry": {
                "agent_id": p["op"]["agent_id"],
                "tools": [
                    {"component_id": "eg.graph.GetNodesByLabel"},
                    {"component_id": "mail.send"},
                ],
            }
        },
    )
    engine.install(surface)
    engine.install(agent_library)
    engine.install(model_route)
    monkeypatch.setattr("arbora.security.session.use_session", _noop_session)
    a2a = _A2A()
    run = next(op for op in agents.operations() if op.id == "agents.run")
    message = {"kind": "message", "metadata": {"role_instance_id": "role:one"}}
    await agents.handle_agent(
        _run_context("tenant:one", a2a), {"message": message}, run
    )
    sent = a2a.messages[0]["metadata"]
    assert "eg.graph.GetNodesByLabel" in sent["tool_allowlist"]
    assert sent["tool_routes"] == {
        "eg.graph.GetNodesByLabel": "engine",
        "mail.send": "tool",
    }
