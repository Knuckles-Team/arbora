"""Role template and instance operations (ARBORA-OPS-R917.1, R917.2)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import operator_surface as surface
from tests.api._engine_double import EngineDouble, context

_OPS = {op.id: op for op in surface.operations()}


@pytest.mark.spec("ARBORA-OPS-R917.1")
async def test_templates_list_reads_engine_library_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed = _OPS["roles.templates.list"]
    assert listed.scopes == frozenset({"registry:read"})
    # The pinned engine has the template method but no tenant-wide list shape.
    with pytest.raises(OperationRefused) as pinned:
        await surface.handle_engine(context(), {"limit": 10, "cursor": None}, listed)
    assert pinned.value.code == "UNAVAILABLE"
    assert pinned.value.details["missing"] == "AgentTemplateOpList"
    engine = EngineDouble(monkeypatch)
    engine.serve(
        "AgentTemplate",
        lambda p: (
            {"templates": [{"template_id": "t1"}], "next_cursor": "c2"}
            if p["op"]["op"] == "list"
            else {"entry": {"template_id": p["op"]["template_id"], "digest": "d"}}
            if p["op"]["template_id"] == "t1"
            else {}
        ),
        "AgentTemplateOpList",
    )
    engine.install()
    page = await surface.handle_engine(context(), {"limit": 10, "cursor": None}, listed)
    assert page == {"templates": [{"template_id": "t1"}], "next_cursor": "c2"}
    assert engine.sent[0][1]["op"]["tenant_id"] == "tenant:one"
    got = await surface.handle_engine(
        context(), {"template_id": "t1"}, _OPS["roles.templates.get"]
    )
    assert got["value"]["digest"] == "d"
    with pytest.raises(OperationRefused) as unknown:
        await surface.handle_engine(
            context(), {"template_id": "nope"}, _OPS["roles.templates.get"]
        )
    assert unknown.value.code == "NOT_FOUND"


@pytest.mark.spec("ARBORA-OPS-R917.2")
async def test_instance_binds_template_to_profile_and_refuses_widening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create = _OPS["roles.instances.create"]
    assert create.scopes == frozenset({"agent:component-write"})
    params: dict[str, Any] = {
        "template_id": "t1",
        "binding": {"instance_id": "role:one", "bindings": {}},
    }
    # No profile method on the pinned engine: the write refuses naming it.
    with pytest.raises(OperationRefused) as pinned:
        await surface.handle_instance_bind(context(), params, create)
    assert pinned.value.details["missing"] == "BusinessProfile"

    profiles: dict[str, Any] = {}
    engine = EngineDouble(monkeypatch)
    engine.serve(
        "BusinessProfile",
        lambda p: {"profile": profiles.get(p["op"]["tenant_id"])},
    )

    def template(p: dict[str, Any]) -> Any:
        request = p["op"]["request"]
        if request["bindings"] == {"widen": True}:
            raise EngineRefusal("BINDING_WIDENS_TEMPLATE")
        return {"entry": {"agent_id": request["agent_id"], "entry_revision": 1}}

    engine.serve("AgentTemplate", template)
    engine.install()
    with pytest.raises(OperationRefused) as unset:
        await surface.handle_instance_bind(context(), params, create)
    assert unset.value.code == "PROFILE_UNSET"
    profiles["tenant:one"] = {"entity": "e"}
    # The other tenant still has no profile: its answer is the same refusal.
    with pytest.raises(OperationRefused) as other:
        await surface.handle_instance_bind(context("tenant:two"), params, create)
    assert other.value.code == "PROFILE_UNSET"
    bound = await surface.handle_instance_bind(context(), params, create)
    assert bound["value"]["agent_id"] == "role:one"
    sent = [s for s in engine.sent if s[0] == "AgentTemplate"][-1]
    assert sent[1]["op"]["request"]["tenant_id"] == "tenant:one" and sent[2] is True
    with pytest.raises(EngineRefusal) as widening:
        await surface.handle_instance_bind(
            context(),
            {
                "template_id": "t1",
                "binding": {"instance_id": "role:one", "bindings": {"widen": True}},
            },
            create,
        )
    assert widening.value.code == "BINDING_WIDENS_TEMPLATE"
    with pytest.raises(OperationRefused) as nameless:
        await surface.handle_instance_bind(
            context(), {"template_id": "t1", "binding": {}}, create
        )
    assert nameless.value.code == "INVALID_ARGUMENT"
