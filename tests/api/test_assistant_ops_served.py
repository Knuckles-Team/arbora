"""Assistant profile and document set operations (ARBORA-OPS-R897.6)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.ops import assistants
from tests.api._knowledge_support import NO_OP, caller_context


def _record(monkeypatch: pytest.MonkeyPatch, answer: Any = None) -> list[Any]:
    sent: list[Any] = []

    async def engine_send(
        context: Any, method: str, params: dict[str, Any], *, idempotent: bool = False
    ) -> Any:
        sent.append((method, params, idempotent))
        if isinstance(answer, Exception):
            raise answer
        return {"ok": True}

    monkeypatch.setattr(assistants, "engine_send", engine_send)
    return sent


async def test_profile_and_document_set_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _record(monkeypatch)
    context = caller_context()
    await assistants.handle_profile_put(
        context,
        {
            "profile_id": "p1",
            "name": "n",
            "instructions": "be brief",
            "tool_allowlist": ["a.b"],
            "expected_revision": 3,
        },
        NO_OP,
    )
    method, params, idempotent = sent[0]
    profile = params["op"]["profile"]
    assert (method, idempotent, params["op"]["op"]) == (
        "AssistantProfile",
        True,
        "put_profile",
    )
    assert profile["revision"] == 3 and profile["tenant"] == "tenant:one"
    assert profile["owner_principal"] == "person:one"
    assert profile["model_hints"] == {
        "preferred_model_profile_refs": [],
        "max_context_tokens": None,
    }
    await assistants.handle_document_set_put(
        context, {"set_id": "d1", "name": "n", "members": ["m"]}, NO_OP
    )
    assert sent[1][1]["op"]["set"]["kind"] == {"members": ["m"]}
    await assistants.handle_document_set_put(
        context, {"set_id": "d2", "name": "n", "saved_query": "MATCH"}, NO_OP
    )
    assert sent[2][1]["op"]["set"]["kind"] == {"saved_query": "MATCH"}
    for handler, body, name in (
        (assistants.handle_profile_get, {"profile_id": "p1"}, "get_profile"),
        (assistants.handle_profile_list, {}, "list_profiles"),
        (assistants.handle_document_set_get, {"set_id": "d1"}, "get_document_set"),
        (assistants.handle_document_set_list, {}, "list_document_sets"),
    ):
        await handler(context, body, NO_OP)
        assert sent[-1][1]["op"]["op"] == name and sent[-1][2] is False
    with pytest.raises(ValidationError):
        assistants.DocumentSetPutParams(set_id="d", name="n")
    with pytest.raises(ValidationError):
        assistants.DocumentSetPutParams(
            set_id="d", name="n", members=["m"], saved_query="q"
        )
    _record(monkeypatch, EngineRefusal("PROFILE_REVISION_CONFLICT"))
    with pytest.raises(EngineRefusal) as conflict:
        await assistants.handle_profile_put(
            context, {"profile_id": "p1", "name": "n", "instructions": ""}, NO_OP
        )
    assert conflict.value.code == "PROFILE_REVISION_CONFLICT"
