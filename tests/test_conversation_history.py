"""Caller/context isolation over the actual native-client history adapter seam."""

import contextlib
import hashlib
from copy import deepcopy
from types import SimpleNamespace

import pytest

from arbora import conversation_history as history


def row(item="prior", run="prior-run", updated=10):
    return {
        "work_item_id": item,
        "kind": "orchestrator_task",
        "status": "succeeded",
        "updated_at_ms": updated,
        "metadata": {
            "a2a_schema": "arbora-a2a-unary-v1",
            "a2a_owner_ref": "owner",
            "a2a_context_id": "context",
            "a2a_run_id": run,
        },
    }


@pytest.fixture
def binding(monkeypatch):
    value = SimpleNamespace(
        session=SimpleNamespace(tenant="tenant"),
        context_id="context",
        owner_ref="owner",
        history_required=True,
        work_item_id="current",
        run_id="current-run",
        require_active=lambda: None,
    )
    monkeypatch.setattr(history, "current_admitted_claim", lambda _: value)
    monkeypatch.setattr(history, "use_session", lambda _: contextlib.nullcontext())
    return value


def bind_client(binding, pages, checkpoint=None):
    calls = []

    async def listing(**params):
        calls.append(("list", params))
        return pages.pop(0)

    async def get_checkpoint(**params):
        calls.append(("checkpoint", params))
        return checkpoint

    client = SimpleNamespace(list=listing, get_checkpoint=get_checkpoint)
    binding.engine = SimpleNamespace(
        graph_compute=SimpleNamespace(
            for_graph=lambda _: SimpleNamespace(
                async_client=SimpleNamespace(work_items=client)
            )
        )
    )
    return calls, client


def snapshot(item):
    return {
        "work_item": item,
        "revision": 2,
        "content": "native-content",
        "payload_digest": hashlib.sha256(b"native-content").hexdigest(),
    }


@pytest.mark.asyncio
async def test_latest_completed_history_survives_empty_native_pages(binding):
    selected = row("latest", "latest-run", 20)
    calls, _ = bind_client(
        binding,
        [
            {
                "items": [row(), row("current", "current-run", 30)],
                "next_cursor": "next",
            },
            {"items": [], "next_cursor": "last"},
            {"items": [selected], "next_cursor": None},
        ],
        snapshot(selected),
    )
    assert await history.read_conversation_history(object()) == {
        "run_id": "latest-run",
        "content": "native-content",
    }
    assert calls[-1] == (
        "checkpoint",
        {"tenant": "tenant", "work_item_id": "latest", "run_id": "latest-run"},
    )
    assert calls[0][1]["metadata_match"] == {
        "a2a_schema": "arbora-a2a-unary-v1",
        "a2a_context_id": "context",
        "a2a_owner_ref": "owner",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"a2a_owner_ref": "foreign"},
        {"a2a_context_id": "foreign"},
        {"a2a_run_id": "foreign"},
    ],
)
async def test_same_snapshot_binding_rechecked_before_content_returns(binding, changes):
    listed = row()
    current = deepcopy(listed)
    current["metadata"].update(changes)
    bind_client(binding, [{"items": [listed], "next_cursor": None}], snapshot(current))
    with pytest.raises(history.ConversationHistoryUnavailable):
        await history.read_conversation_history(object())


@pytest.mark.asyncio
async def test_foreign_list_row_performs_no_checkpoint_read(binding):
    foreign = row()
    foreign["metadata"]["a2a_owner_ref"] = "foreign"
    calls, _ = bind_client(binding, [{"items": [foreign], "next_cursor": None}])
    with pytest.raises(history.ConversationHistoryUnavailable):
        await history.read_conversation_history(object())
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True])
async def test_absent_history_only_allowed_for_fresh_context(binding, required):
    binding.history_required = required
    bind_client(binding, [{"items": [], "next_cursor": None}])
    if required:
        with pytest.raises(history.ConversationHistoryUnavailable):
            await history.read_conversation_history(object())
    else:
        assert await history.read_conversation_history(object()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "digest", "closed"])
async def test_checkpoint_unavailable_or_stale_binding_never_returns_history(
    binding, failure
):
    item = row()
    value = snapshot(item)
    if failure == "digest":
        value["payload_digest"] = "wrong"
    calls, client = bind_client(
        binding,
        [{"items": [item], "next_cursor": None}],
        None if failure == "missing" else value,
    )
    if failure == "closed":

        async def read(**params):
            def refuse():
                raise RuntimeError("closed")

            binding.require_active = refuse
            return value

        client.get_checkpoint = read
    with pytest.raises(history.ConversationHistoryUnavailable):
        await history.read_conversation_history(object())


@pytest.mark.asyncio
async def test_equal_newest_timestamps_refuse_ambiguous_turn(binding):
    calls, _ = bind_client(
        binding, [{"items": [row("one"), row("two")], "next_cursor": None}]
    )
    with pytest.raises(history.ConversationHistoryUnavailable):
        await history.read_conversation_history(object())
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_repeating_cursor_refuses_without_checkpoint_read(binding):
    calls, _ = bind_client(
        binding,
        [{"items": [], "next_cursor": "again"}, {"items": [], "next_cursor": "again"}],
    )
    with pytest.raises(history.ConversationHistoryUnavailable):
        await history.read_conversation_history(object())
    assert len(calls) == 2


def test_required_history_refuses_older_harness(monkeypatch):
    from agent_utilities import host_ports

    monkeypatch.setattr(
        history, "admitted_claim_for", lambda *_: SimpleNamespace(history_required=True)
    )
    monkeypatch.delattr(host_ports, "register_run_history_provider", raising=False)
    with pytest.raises(history.ConversationHistoryUnavailable):
        history.require_history_support("current", object())


def test_optional_history_allows_older_harness(monkeypatch):
    monkeypatch.setattr(
        history,
        "admitted_claim_for",
        lambda *_: SimpleNamespace(history_required=False),
    )
    history.require_history_support("current", object())
