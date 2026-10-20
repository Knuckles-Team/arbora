"""Canonical context identity uses the existing durable WorkItem reader."""

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.a2a.models import A2AMessage, A2ATextPart

CONTEXT = "a2a-context-" + "a" * 64
SESSION = SimpleNamespace(tenant="tenant", graph="graph")


def message(context: str = CONTEXT) -> A2AMessage:
    return A2AMessage(
        role="user",
        parts=[A2ATextPart(text="next")],
        message_id="next",
        context_id=context,
    )


def row() -> dict[str, Any]:
    return {
        "work_item_id": "work-item:" + "b" * 64,
        "status": "succeeded",
        "input_ref": "input",
        "version": 1,
        "updated_at_ms": 1,
        "kind": "orchestrator_task",
        "metadata": {
            "a2a_schema": "arbora-a2a-unary-v1",
            "a2a_owner_ref": "owner",
            "a2a_context_id": CONTEXT,
        },
    }


def reader(monkeypatch: pytest.MonkeyPatch, pages: list[Any]) -> tuple[Any, list[Any]]:
    calls: list[Any] = []

    def read(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return pages.pop(0)

    engine = SimpleNamespace(work_items=SimpleNamespace(list=read))
    authority = WorkItemA2AAuthority(lambda: engine)
    monkeypatch.setattr(authority, "_session", lambda scope: SESSION)
    return authority, calls


def test_canonical_context_survives_new_authority_and_empty_page(
    monkeypatch: Any,
) -> None:
    for _ in range(2):
        authority, calls = reader(
            monkeypatch,
            [
                {"items": [], "next_cursor": "more"},
                {"items": [row()], "next_cursor": None},
            ],
        )
        assert (
            authority._resolve_context_id(SESSION, message(), "new-task", "owner")
            == CONTEXT
        )
        assert calls[1]["cursor"] == "more"
        assert calls[0]["metadata_match"] == row()["metadata"]
        assert calls[0]["tenant"] == "tenant"


@pytest.mark.parametrize("field,value", [("kind", "foreign"), ("metadata", {})])
def test_foreign_record_is_refused(monkeypatch: Any, field: str, value: Any) -> None:
    record = row()
    record[field] = value
    authority, _ = reader(monkeypatch, [{"items": [record]}])
    with pytest.raises(PermissionError):
        authority._resolve_context_id(SESSION, message(), "new-task", "owner")


@pytest.mark.parametrize(
    "pages",
    [
        [{"items": [], "next_cursor": None}],
        [{"items": [], "next_cursor": "same"}] * 2,
        [{"items": [], "next_cursor": str(i)} for i in range(32)],
    ],
)
def test_unknown_or_unbounded_context_never_resolves(
    monkeypatch: Any, pages: Any
) -> None:
    authority, _ = reader(monkeypatch, pages)
    with pytest.raises((PermissionError, RuntimeError)):
        authority._resolve_context_id(SESSION, message(), "new-task", "owner")


def test_changed_caller_after_read_refuses(monkeypatch: Any) -> None:
    authority, _ = reader(monkeypatch, [{"items": [row()]}])
    monkeypatch.setattr(
        authority, "_session", lambda scope: SimpleNamespace(tenant="other")
    )
    with pytest.raises(PermissionError, match="caller changed"):
        authority._resolve_context_id(SESSION, message(), "new-task", "owner")


@pytest.mark.asyncio
async def test_foreign_context_never_submits_or_executes(monkeypatch: Any) -> None:
    from arbora.a2a.models import A2ARouteDecision

    authority, _ = reader(monkeypatch, [{"items": [], "next_cursor": None}])
    monkeypatch.setattr(authority, "_owner_ref", lambda session: "owner")
    monkeypatch.setattr(authority, "_submit", lambda *a, **kw: pytest.fail("admitted"))
    with pytest.raises(PermissionError):
        await authority.dispatch(
            message=message(),
            idempotency_key="key",
            decision=A2ARouteDecision(agent_name="arbora", selection_mode="agent"),
        )


def test_new_context_keeps_existing_deterministic_identity(monkeypatch: Any) -> None:
    authority, calls = reader(monkeypatch, [])
    request = message("client-conversation")
    expected = authority._context_id(SESSION, request, "task", "owner")
    assert authority._resolve_context_id(SESSION, request, "task", "owner") == expected
    assert calls == []


@pytest.mark.parametrize("context", ["a2a-context-x", "a2a-context-" + "Z" * 64])
def test_malformed_canonical_context_does_not_read(
    monkeypatch: Any, context: str
) -> None:
    authority, calls = reader(monkeypatch, [])
    with pytest.raises(ValueError):
        authority._resolve_context_id(SESSION, message(context), "task", "owner")
    assert calls == []


def test_fixture_matches_published_eg_caller_view() -> None:
    from epistemic_graph.client import _work_item_view

    assert _work_item_view(row()) == row()
