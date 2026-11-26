"""One role template serves two tenants over isolated business profiles (ARBORA-OPS-R917.4)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import agents
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
    profiles = {"tenant:one": {"entity": "one"}, "tenant:two": {"entity": "two"}}
    engine = EngineDouble(monkeypatch)
    engine.serve(
        "KvGet",
        lambda p: (
            {"value": json.dumps(profiles[engine.tenant]).encode(), "version": 7}
            if engine.tenant in profiles
            else None
        ),
    )
    engine.install(surface)
    monkeypatch.setattr("arbora.security.session.use_session", _noop_session)
    a2a = _A2A()
    run = next(op for op in agents.operations() if op.id == "agents.run")
    message = {"kind": "message", "metadata": {"role_instance_id": "role:one"}}
    for tenant in ("tenant:one", "tenant:two"):
        await agents.handle_agent(_run_context(tenant, a2a), {"message": message}, run)
    # Each run carries only its own tenant's profile reference, read by that tenant.
    assert [sent[0] for sent in engine.sent] == ["KvGet", "KvGet"]
    assert all(m["metadata"]["profile_ref"].endswith("@7") for m in a2a.messages)
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
