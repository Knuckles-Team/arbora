"""Chat history operations scoped to the caller (ARBORA-OPS-R909)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import chats
from tests.api._knowledge_support import NO_OP

pytestmark = pytest.mark.spec("ARBORA-OPS-R909")


class MemoryEngine:
    """A recording memory-file engine: revisions, conflicts, one doc per id."""

    def __init__(self) -> None:
        self.docs: dict[str, tuple[str, int]] = {}
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.replays: dict[str, Any] = {}

    async def __call__(
        self,
        context: Any,
        method: str,
        params: dict[str, Any],
        *,
        idempotent: bool = False,
    ) -> Any:
        self.sent.append((method, params))
        if method == "ReadMemorySnapshot":
            doc = self.docs.get(params["doc_id"])
            if doc is None:
                from arbora.api.errors import EngineRefusal

                raise EngineRefusal("NOT_FOUND")
            return {"content": doc[0], "head": {"revision": doc[1]}}
        if method == "ListMemorySnapshots":
            heads = [
                {"doc_id": doc_id, "revision": rev}
                for doc_id, (_, rev) in sorted(self.docs.items())
                if doc_id.startswith(params["prefix"])
            ]
            return {"heads": heads, "next_cursor": None}
        request = params["request"]
        if request["request_id"] in self.replays:
            return self.replays[request["request_id"]]
        action, doc_id = request["action"], request["doc_id"]
        current = self.docs.get(doc_id)
        expected = action.get("expected_revision")
        head_revision = current[1] if current else 0
        if expected is not None and expected != head_revision:
            return {"outcome": "conflict_retained", "head": {"revision": head_revision}}
        assert (
            hashlib.sha256(action["content"].encode()).hexdigest()
            == action["content_digest"]
        )
        self.docs[doc_id] = (action["content"], head_revision + 1)
        outcome = {"outcome": "accepted", "head": {"revision": head_revision + 1}}
        self.replays[request["request_id"]] = outcome
        return outcome


class _Context:
    """A caller context whose idempotency key is fresh on every request."""

    def __init__(self, principal: str, fixed: str | None = None) -> None:
        from types import SimpleNamespace

        self.caller = SimpleNamespace(tenant="tenant:one", principal=principal)
        self._fixed = fixed
        self._n = 0

    @property
    def idempotency_key(self) -> str:
        if self._fixed is not None:
            return self._fixed
        self._n += 1
        return f"{self.caller.principal}:{self._n}"


def _context(principal: str, key: str | None = None) -> Any:
    return _Context(principal, key)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> MemoryEngine:
    fake = MemoryEngine()
    monkeypatch.setattr(chats, "engine_send", fake)
    monkeypatch.setattr(chats, "now", lambda: datetime(2026, 11, 26, tzinfo=UTC))
    return fake


async def test_chat_rows_are_scoped_to_the_caller(engine: MemoryEngine) -> None:
    mine, theirs = _context("person:a"), _context("person:b", key="k2")
    made = await chats.handle_chats_create(mine, {"title": "t", "messages": []}, NO_OP)
    chat_id = made["value"]["chat"]["id"]
    await chats.handle_chats_create(theirs, {"title": "other"}, NO_OP)
    assert all(
        d.startswith(("chats/person:a/", "chats/person:b/")) for d in engine.docs
    )
    listed = await chats.handle_chats_list(mine, {}, NO_OP)
    assert [c["id"] for c in listed["value"]["chats"]] == [chat_id]
    got = await chats.handle_chats_get(mine, {"chat_id": chat_id}, NO_OP)
    assert got["value"]["chat"]["title"] == "t"
    assert got["value"]["chat"]["updated_at"] == "2026-11-26T00:00:00.000Z"
    assert chats.operations()[0].scopes == frozenset({"chats:own"})
    # A replayed create (the same key) is the same chat.
    replay = _context("person:c", key="fixed")
    first = await chats.handle_chats_create(replay, {"title": "t"}, NO_OP)
    again = await chats.handle_chats_create(replay, {"title": "t"}, NO_OP)
    assert again["value"]["chat"]["id"] == first["value"]["chat"]["id"]


async def test_stale_revision_is_refused(engine: MemoryEngine) -> None:
    context = _context("person:a")
    made = await chats.handle_chats_create(context, {"title": "t"}, NO_OP)
    chat_id = made["value"]["chat"]["id"]
    updated = await chats.handle_chats_update(
        context, {"chat_id": chat_id, "title": "u", "expected_revision": 1}, NO_OP
    )
    assert updated["value"]["chat"]["revision"] == 2
    with pytest.raises(OperationRefused) as refused:
        await chats.handle_chats_update(
            context, {"chat_id": chat_id, "title": "v", "expected_revision": 1}, NO_OP
        )
    assert refused.value.code == "REVISION_CONFLICT"


async def test_other_subject_chat_is_not_found(engine: MemoryEngine) -> None:
    made = await chats.handle_chats_create(_context("person:a"), {"title": "t"}, NO_OP)
    chat_id = made["value"]["chat"]["id"]
    other = _context("person:b")
    for handler, params in (
        (chats.handle_chats_get, {"chat_id": chat_id}),
        (chats.handle_chats_delete, {"chat_id": chat_id}),
        (chats.handle_chats_update, {"chat_id": chat_id, "expected_revision": 1}),
    ):
        with pytest.raises(OperationRefused) as refused:
            await handler(other, params, NO_OP)
        assert refused.value.code == "NOT_FOUND"


async def test_delete_writes_a_tombstone_that_reads_as_absent(
    engine: MemoryEngine,
) -> None:
    context = _context("person:a")
    made = await chats.handle_chats_create(context, {"title": "t"}, NO_OP)
    chat_id = made["value"]["chat"]["id"]
    deleted = await chats.handle_chats_delete(context, {"chat_id": chat_id}, NO_OP)
    assert deleted["value"] == {"deleted": True}
    assert json.loads(engine.docs[f"chats/person:a/{chat_id}"][0])["deleted"] is True
    with pytest.raises(OperationRefused):
        await chats.handle_chats_get(context, {"chat_id": chat_id}, NO_OP)
    listed = await chats.handle_chats_list(context, {}, NO_OP)
    assert listed["value"]["chats"] == []


async def test_oversize_message_is_refused(engine: MemoryEngine) -> None:
    big = {"role": "user", "content": "x" * (chats.MAX_MESSAGE_BYTES + 1)}
    with pytest.raises(OperationRefused) as refused:
        await chats.handle_chats_create(
            _context("person:a"), {"messages": [big]}, NO_OP
        )
    assert refused.value.code == "INVALID_ARGUMENT"
    assert engine.sent == []
