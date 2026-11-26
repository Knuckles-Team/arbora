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


@pytest.mark.spec("ARBORA-OPS-R897.6")
async def test_profile_and_document_set_round_trip_and_run_reference(
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
    await _run_carries_the_profile_reference(monkeypatch)


async def _run_carries_the_profile_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    from arbora.api.ops import agents

    calls: list[dict[str, Any]] = []

    class Service:
        async def send_message(self, **params: Any) -> SimpleNamespace:
            calls.append(params)
            return SimpleNamespace(model_dump=lambda **_kw: {"id": "task-1"})

    op = next(op for op in agents.operations() if op.id == "agents.run")
    context = caller_context()
    context.caller.session = object()
    context.idempotency_key = "key-1"
    context.services = {"a2a": Service()}
    import arbora.security.session as session

    monkeypatch.setattr(session, "use_session", lambda _session: nullcontext())
    await agents.handle_agent(
        context,
        {
            "message": {"parts": [], "metadata": {"agent_name": "a"}},
            "assistant_profile_ref": "profile:p1",
        },
        op,
    )
    await agents.handle_agent(context, {"message": {"parts": []}}, op)
    # The reference rides in the message metadata beside the caller's own; a run
    # with none sends the message unchanged.
    assert calls[0]["message"]["metadata"] == {
        "agent_name": "a",
        "assistant_profile_ref": "profile:p1",
    }
    assert calls[1]["message"] == {"parts": []}
