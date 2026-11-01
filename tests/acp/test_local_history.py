"""Bounded process-only history over the existing Pydantic store and AU codec."""

import json

import pytest
from meristem.harness.conversation_history import decode_completed_history
from meristem.runtime import RunRequest
from pydantic_ai_harness.experimental.acp import InMemorySessionStore

from arbora.acp.local_history import LocalConversationHistory
from arbora.acp.session_policy import PrincipalContext

pytestmark = pytest.mark.spec("ARBORA-ACP-R001.9.1")
PRINCIPAL = PrincipalContext("user", "tenant", frozenset({"kg:write"}))


def result(request, output="answer"):
    return {"run_id": request.run_id, "status": "completed", "output": output}


async def test_completed_text_history_is_bound_to_actual_predecessor_and_session():
    history = LocalConversationHistory("model:test")
    first = await history.request(PRINCIPAL, "a", "first")
    assert await history.read(first) is None
    await history.complete(first, result(first))
    second = await history.request(PRINCIPAL, "a", "next")
    content = await history.read(second)
    envelope = json.loads(content)
    assert envelope["run_id"] == first.run_id != second.run_id
    assert envelope["messages"] == [
        {"role": "user", "text": "first"},
        {"role": "assistant", "text": "answer"},
    ]
    assert len(decode_completed_history(content, run_id=second.run_id)) == 2
    other = await history.request(PRINCIPAL, "b", "other")
    assert await history.read(other) is None
    forged = RunRequest(task="forged", metadata=second.metadata)
    with pytest.raises(PermissionError):
        await history.read(forged)


async def test_failed_and_cancelled_turns_do_not_replace_completed_history():
    history = LocalConversationHistory("model:test")
    first = await history.request(PRINCIPAL, "a", "first")
    await history.complete(first, result(first))
    cancelled = await history.request(PRINCIPAL, "a", "cancelled")
    with pytest.raises(ValueError):
        await history.complete(
            cancelled,
            {"run_id": cancelled.run_id, "status": "failed", "output": "partial"},
        )
    after = await history.request(PRINCIPAL, "a", "after")
    assert json.loads(await history.read(after))["run_id"] == first.run_id
    with pytest.raises(PermissionError):
        await history.read(cancelled)


async def test_save_failure_poisons_continuation_without_replay(monkeypatch):
    history = LocalConversationHistory("model:test")
    request = await history.request(PRINCIPAL, "a", "first")
    original = InMemorySessionStore.save
    calls = []

    async def fail_after_write(self, session_id, stored):
        calls.append(session_id)
        await original(self, session_id, stored)
        raise RuntimeError("private storage detail")

    monkeypatch.setattr(InMemorySessionStore, "save", fail_after_write)
    with pytest.raises(RuntimeError):
        await history.complete(request, result(request))
    with pytest.raises(PermissionError):
        await history.request(PRINCIPAL, "a", "again")
    assert calls == ["a"]
    await history.close("a")
    fresh = await history.request(PRINCIPAL, "a", "fresh")
    assert await history.read(fresh) is None


async def test_session_turn_byte_limits_and_close_cleanup():
    history = LocalConversationHistory("model:test", max_sessions=1, max_turns=1)
    first = await history.request(PRINCIPAL, "a", "first")
    with pytest.raises(PermissionError):
        await history.request(PRINCIPAL, "b", "other")
    await history.complete(first, result(first))
    with pytest.raises(PermissionError):
        await history.request(PRINCIPAL, "a", "second")
    with pytest.raises(PermissionError):
        await history.request(
            PrincipalContext("other", "tenant", PRINCIPAL.scopes), "a", "foreign"
        )
    await history.close("a")
    with pytest.raises(PermissionError):
        await history.read(first)
    fresh = await history.request(PRINCIPAL, "b", "new")
    assert await history.read(fresh) is None
    tiny = LocalConversationHistory("model:test", max_bytes=32)
    with pytest.raises(PermissionError):
        await tiny.request(PRINCIPAL, "a", "first")
