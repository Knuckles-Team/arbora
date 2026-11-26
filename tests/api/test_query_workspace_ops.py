"""The query workspace operations over the engine's stored-query methods (ARBORA-OPS-R855.3)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import governance


def _ops() -> dict[str, Any]:
    return {op.id: op for op in governance.operations()}


# spec: ARBORA-OPS-R855.3
def test_workspace_operations_map_one_to_one_onto_the_stored_query_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, dict[str, Any], bool]] = []

    async def fake_send(
        _context: Any, method: str, params: dict[str, Any], *, idempotent: bool = False
    ) -> dict[str, Any]:
        sent.append((method, params, idempotent))
        return {"ok": True}

    monkeypatch.setattr(governance, "engine_send", fake_send)
    ops = _ops()
    assert {i for i in ops if i.startswith("query.workspace.")} == {
        "query.workspace.save",
        "query.workspace.get",
        "query.workspace.list",
        "query.workspace.delete",
    }

    async def drive() -> None:
        put = ops["query.workspace.save"].params.model_validate(
            {"name": "weekly", "language": "uql", "text": "MATCH (n) RETURN n"}
        )
        await governance.handle_workspace_put(
            None, put.model_dump(), ops["query.workspace.save"]
        )
        await governance.handle_workspace_get(
            None, {"name": "weekly", "version": 2}, ops["query.workspace.get"]
        )
        await governance.handle_workspace_list(
            None, {"include_retired": True, "limit": 5}, ops["query.workspace.list"]
        )
        await governance.handle_workspace_delete(
            None,
            {"name": "weekly", "reason": "superseded"},
            ops["query.workspace.delete"],
        )

    asyncio.run(drive())

    assert [call[0] for call in sent] == [
        "StoredQueryPut",
        "StoredQueryGet",
        "StoredQueryList",
        "StoredQueryRetire",
    ]
    query = sent[0][1]["query"]
    assert query["name"] == "weekly" and "author" not in query
    assert query["refresh"] == {"kind": "on_change"}
    assert sent[1][1] == {"name": "weekly", "version": 2}
    assert sent[2][1] == {"include_retired": True, "limit": 5}
    assert sent[3][1] == {"name": "weekly", "reason": "superseded"}
    assert ops["query.workspace.save"].scopes == {"query:write"}
    assert ops["query.workspace.get"].scopes == {"query:read"}


# spec: ARBORA-OPS-R855.3
def test_a_method_the_pinned_engine_lacks_refuses_unavailable_and_sends_nothing() -> (
    None
):
    from medulla.generated import METHOD_IDS

    missing = [
        method
        for method in ("StoredQueryGet", "StoredQueryList", "StoredQueryRetire")
        if method not in METHOD_IDS
    ]
    if not missing:
        pytest.skip("the pinned engine serves the stored-query reads")
    context = SimpleNamespace(client=None, caller=None, idempotency_key=None)
    ops = _ops()
    calls = {
        "StoredQueryGet": (governance.handle_workspace_get, {"name": "q"}, "get"),
        "StoredQueryList": (governance.handle_workspace_list, {}, "list"),
        "StoredQueryRetire": (
            governance.handle_workspace_delete,
            {"name": "q", "reason": "unsafe"},
            "delete",
        ),
    }
    for method in missing:
        handler, params, name = calls[method]
        with pytest.raises(OperationRefused) as refused:
            asyncio.run(handler(context, params, ops[f"query.workspace.{name}"]))
        assert refused.value.code == "UNAVAILABLE"
