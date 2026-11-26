"""Knowledge flag operations (ARBORA-OPS-R950)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.ops import knowledge
from arbora.api.registry.pending_scopes import load_pending_engine_scopes
from tests.api._knowledge_support import caller_context, record_engine
from tests.test_served_operation_api import _call, served  # noqa: F401


@pytest.mark.spec("ARBORA-OPS-R950")
async def test_raise_resolve_get_list_forward_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = record_engine(monkeypatch, knowledge, {"flag_id": "f1"})
    context = caller_context()
    raised = await knowledge.handle_flag_raise(
        context,
        {
            "knowledge_kind": "skill",
            "flag_kind": "wrong",
            "target_ref": "skill:x@1",
            "what_observed": "it failed",
            "run_id": "r1",
            "at_ms": 5,
            "suggested_correction": None,
        },
        None,
    )
    assert raised == {"value": {"flag_id": "f1"}}
    await knowledge.handle_flag_resolve(
        context, {"flag_id": "f1", "resolution": "adopt"}, None
    )
    await knowledge.handle_flag_get(context, {"flag_id": "f1"}, None)
    await knowledge.handle_flag_list(context, {"state": "open", "limit": None}, None)
    assert sent == [
        (
            "KnowledgeFlag",
            {
                "op": "raise",
                "knowledge_kind": "skill",
                "flag_kind": "wrong",
                "target_ref": "skill:x@1",
                "what_observed": "it failed",
                "run_id": "r1",
                "at_ms": 5,
            },
            True,
        ),
        (
            "KnowledgeFlag",
            {"op": "resolve", "flag_id": "f1", "resolution": "adopt"},
            True,
        ),
        ("KnowledgeFlag", {"op": "get", "flag_id": "f1"}, False),
        ("KnowledgeFlag", {"op": "list", "state": "open"}, False),
    ]
    # No body field can name the tenant or the actor.
    assert not {"tenant", "graph", "actor"} & set(
        knowledge.FlagRaiseParams.model_fields
    )


@pytest.mark.spec("ARBORA-OPS-R950")
def test_flag_operations_are_served_unavailable_until_the_scope_is_pinned(
    served: Any,  # noqa: F811
) -> None:
    app, _client = served
    assert {"knowledge:flag", "knowledge:curate"} <= load_pending_engine_scopes()
    for op_id, params in (
        ("knowledge.flag.get", {"flag_id": "f1"}),
        ("knowledge.flag.list", {}),
    ):
        reply = _call(app, op_id, params)
        assert reply.status_code == 503, reply.text
        assert reply.json()["error"]["code"] == "UNAVAILABLE"
