"""Agent Library operations over the engine AgentLibrary method (ARBORA-OPS-R912)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import agent_library
from arbora.api.ops.registry_factory import get_registry

pytestmark = pytest.mark.spec("ARBORA-OPS-R912")


def _dep(kind: str, name: str) -> dict[str, str]:
    return {
        "component_id": name,
        "definition_digest": "sha256:" + "a" * 64,
        "kind": kind,
    }


def _item(agent_id: str = "agent:one") -> dict[str, Any]:
    return {
        "actor_scope": "scope",
        "agent_id": agent_id,
        "model_identity": "model",
        "model_profile": _dep("model_profile", "profile"),
        "ontologies": [],
        "package_id": "pkg",
        "policy_digest": "sha256:" + "b" * 64,
        "purpose_id": "purpose",
        "role": "researcher",
        "role_digest": "sha256:" + "c" * 64,
        "skills": [_dep("skill", "skill:search")],
        "source_revision": "r1",
        "source_revision_digest": "sha256:" + "d" * 64,
        "system_prompt": _dep("system_prompt", "prompt:one"),
        "tools": [],
        "version": "1.0.0",
    }


def _context() -> Any:
    session = SimpleNamespace(
        actor=SimpleNamespace(actor_id="person:one"),
        tenant="tenant:one",
        policy_version="p1",
        trace_context="trace",
    )
    caller = SimpleNamespace(
        tenant="tenant:one", principal="person:one", session=session
    )
    return SimpleNamespace(caller=caller, idempotency_key="k")


class FakeLibrary:
    """The engine library: one head entry per agent with revision compare."""

    def __init__(self) -> None:
        self.heads: dict[str, dict[str, Any]] = {}
        self.sent: list[tuple[str, dict[str, Any], bool]] = []

    async def send(
        self,
        context: Any,
        method: str,
        params: dict[str, Any],
        *,
        idempotent: bool = False,
    ) -> Any:
        op = params["op"]
        self.sent.append((method, op, idempotent))
        kind = op["operation"]
        if kind == "publish":
            entry = op["request"]["entry"]
            expected = op["request"]["context"]["expected_revision"]
            head = self.heads.get(entry["agent_id"])
            current = head["entry_revision"] if head else 0
            if expected is not None and expected != current:
                raise OperationRefused("REVISION_CONFLICT")
            stored = {**entry, "entry_revision": current + 1, "lifecycle": "published"}
            self.heads[entry["agent_id"]] = stored
            return {"entry": stored}
        if kind == "retire":
            head = self.heads[op["request"]["agent_id"]]
            head["lifecycle"] = "retired"
            return {"entry": head}
        if kind == "current":
            head = self.heads.get(op["agent_id"])
            return {"entry": head} if head else {}
        if kind == "list":
            rows = sorted(self.heads.values(), key=lambda h: h["agent_id"])
            start = int(op.get("cursor") or 0)
            page = rows[start : start + op["limit"]]
            more = start + op["limit"] < len(rows)
            return {
                "entries": [
                    {k: h[k] for k in ("agent_id", "entry_revision", "lifecycle")}
                    for h in page
                ],
                "next_cursor": str(start + op["limit"]) if more else None,
            }
        raise AssertionError(kind)


@pytest.fixture
def library(monkeypatch: pytest.MonkeyPatch) -> FakeLibrary:
    fake = FakeLibrary()
    monkeypatch.setattr(agent_library, "engine_send", fake.send)
    monkeypatch.setattr(agent_library, "_list_op_served", lambda: True)
    return fake


async def test_create_update_delete_with_revision_conflict(
    library: FakeLibrary,
) -> None:
    context = _context()
    created = await agent_library.handle_agents_library_create(
        context, {"item": _item(), "kind": "agent"}, None
    )
    assert created["value"]["revision"] == 1
    # The draft is bound to the caller's tenant, never to the item's own claim.
    assert library.heads["agent:one"]["tenant_id"] == "tenant:one"
    updated = await agent_library.handle_agents_library_update(
        context, {"item": _item(), "expected_revision": 1}, None
    )
    assert updated["value"]["revision"] == 2
    with pytest.raises(OperationRefused) as stale:
        await agent_library.handle_agents_library_update(
            context, {"item": _item(), "expected_revision": 1}, None
        )
    assert stale.value.code == "REVISION_CONFLICT"
    with pytest.raises(OperationRefused) as blind:
        await agent_library.handle_agents_library_update(
            context, {"item": _item()}, None
        )
    assert blind.value.code == "REVISION_CONFLICT"
    gone = await agent_library.handle_agents_library_delete(
        context, {"id": "agent:one"}, None
    )
    assert gone["value"] == {"id": "agent:one", "retired": True}
    assert library.heads["agent:one"]["lifecycle"] == "retired"
    assert all(idem for _m, op, idem in library.sent if op["operation"] != "current")


async def test_an_invalid_item_is_refused_before_the_engine(
    library: FakeLibrary,
) -> None:
    with pytest.raises(OperationRefused) as refused:
        await agent_library.handle_agents_library_create(
            _context(), {"item": {"agent_id": "x"}}, None
        )
    assert refused.value.code == "INVALID_ARGUMENT"
    assert library.sent == []


async def test_get_a2a_and_absent_agent(library: FakeLibrary) -> None:
    context = _context()
    await agent_library.handle_agents_library_create(context, {"item": _item()}, None)
    got = await agent_library.handle_agents_library_get(
        context, {"id": "agent:one"}, None
    )
    assert got["value"]["items"][0]["agent_id"] == "agent:one"
    card = await agent_library.handle_agents_library_a2a(
        context, {"id": "agent:one"}, None
    )
    assert card["value"]["agent_card"]["skills"] == [
        {"id": "skill:search", "name": "skill:search"}
    ]
    with pytest.raises(OperationRefused) as absent:
        await agent_library.handle_agents_library_get(
            context, {"id": "agent:none"}, None
        )
    assert absent.value.code == "NOT_FOUND"
    await agent_library.handle_agents_library_delete(context, {"id": "agent:one"}, None)
    with pytest.raises(OperationRefused) as retired:
        await agent_library.handle_agents_library_a2a(
            context, {"id": "agent:one"}, None
        )
    assert retired.value.code == "NOT_FOUND"


async def test_list_and_summary_page_through_the_engine(library: FakeLibrary) -> None:
    context = _context()
    for n in range(3):
        await agent_library.handle_agents_library_create(
            context, {"item": _item(f"agent:{n}")}, None
        )
    first = await agent_library.handle_agents_library_list(context, {"limit": 2}, None)
    assert len(first["value"]["items"]) == 2 and first["value"]["next_cursor"] == "2"
    rest = await agent_library.handle_agents_library_list(
        context, {"limit": 2, "cursor": "2"}, None
    )
    assert rest["value"]["next_cursor"] is None
    summary = await agent_library.handle_agents_library_summary(context, {}, None)
    assert summary["value"] == {"counts": {"published": 3}, "total": 3}


async def test_list_is_unavailable_until_the_pinned_engine_serves_it(
    monkeypatch: pytest.MonkeyPatch, library: FakeLibrary
) -> None:
    monkeypatch.setattr(agent_library, "_list_op_served", lambda: False)
    for handler, params in (
        (agent_library.handle_agents_library_list, {}),
        (agent_library.handle_agents_library_summary, {}),
    ):
        with pytest.raises(OperationRefused) as refused:
            await handler(_context(), params, None)
        assert refused.value.code == "UNAVAILABLE"
    assert library.sent == []


@pytest.mark.parametrize("kind", ["tool"])
async def test_other_kinds_are_unavailable_until_the_engine_has_a_kind_read(
    library: FakeLibrary, kind: str
) -> None:
    for handler, params in (
        (agent_library.handle_agents_library_list, {"kind": kind}),
        (agent_library.handle_agents_library_get, {"id": "x", "kind": kind}),
        (agent_library.handle_agents_library_create, {"item": _item(), "kind": kind}),
    ):
        with pytest.raises(OperationRefused) as refused:
            await handler(_context(), params, None)
        assert refused.value.code == "UNAVAILABLE"
    assert library.sent == []


def test_the_seven_operations_are_registered_with_library_scopes() -> None:
    registry = get_registry()
    ids = {s.id: s for s in agent_library.operations()}
    assert sorted(ids) == sorted(
        f"agents.library.{n}"
        for n in ("list", "get", "summary", "a2a", "create", "update", "delete")
    )
    writes = [s for i, s in ids.items() if i.endswith(("create", "update", "delete"))]
    assert all(s.scopes == {"agent:library-write"} for s in writes)
    for op_id in ids:
        assert registry.get(op_id) is not None


def test_a_suggestion_kind_does_not_exist() -> None:
    with pytest.raises(ValidationError):
        agent_library.ListParams(kind="suggestion")
