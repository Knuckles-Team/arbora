"""Consumer source fixtures, not native persistence or restart qualification."""

import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent_utilities import host_ports

from arbora import agent_runs
from arbora.a2a import native_history as history
from arbora.api.invoke.pipeline import OperationRefused
from arbora.mcp_server import harness_host

pytestmark = pytest.mark.spec("ARBORA-A2A-R001.3.3")


@pytest.fixture
def bound(monkeypatch):
    monkeypatch.setattr(agent_runs, "_PRIVATE_TRACE_POLICY", False)
    session = SimpleNamespace(
        tenant="tenant",
        actor=SimpleNamespace(actor_id="person"),
        ensure_authority_current=lambda: None,
        require_scope=lambda scope: None,
    )
    claim = {
        "claimed": True,
        "tenant": "tenant",
        "work_item_id": "work",
        "lease_holder_ref": "holder",
        "lease_epoch": 1,
        "fencing_token": 1,
        "lease_expires_at_ms": 2**63 - 1,
        "fresh_execution_grant": True,
    }
    request = SimpleNamespace(run_id="run", task="next", metadata={})
    owner = SimpleNamespace(
        run_id="run",
        work_item_id="work",
        context_id="context",
        session=session,
        owner_graph="private",
    )
    view = {
        "admission": {
            "run_id": "run",
            "kind": "orchestrator_task",
            "context": {
                "tenant_id": "tenant",
                "subject_id": "person",
                "graph": "private",
            },
            "conversation": {
                "context_id": "context",
                "expected_revision": 1,
                "predecessor_run_id": "previous",
                "history_profile": {
                    "version": 1,
                    "user_text_digest": hashlib.sha256(b"next").hexdigest(),
                },
            },
        },
        "work_item": {"work_item_id": "work", "kind": "orchestrator_task"},
        "checkpoint_selection": {
            "version": 1,
            "run_id": "run",
            "work_item_id": "work",
            "context_id": "context",
            "admission_revision": 2,
            "scope": {
                "tenant": "__shard__",
                "scope": {"kind": "graph", "graph": "private"},
                "incarnation_id": "incarnation",
                "identity_digest": [10] * 32,
            },
            "snapshot_version": 3,
            "current_run_checkpoint": None,
            "completed_history": json.dumps(
                {
                    "version": 1,
                    "phase": "completed",
                    "run_id": "previous",
                    "messages": [
                        {"role": "user", "text": "prior question"},
                        {"role": "assistant", "text": "prior answer"},
                    ],
                }
            ),
            "checkpoints": [
                {
                    "document_id": "inline-checkpoint:work",
                    "revision": 1,
                    "content_digest": "b" * 64,
                }
            ],
        },
    }
    read = AsyncMock(return_value=view)
    monkeypatch.setattr(history, "native_read", read)
    monkeypatch.setattr(agent_runs, "_engine", lambda: object())
    return request, owner, claim, view, read


def scope(bound):
    request, owner, claim, _, _ = bound
    return agent_runs.native_run_scope(
        run_id=request.run_id,
        task_id="task",
        context_id=owner.context_id,
        work_item_id=owner.work_item_id,
        session=owner.session,
        claim=claim,
        owner_graph=owner.owner_graph,
        hosted_invocation=object(),
        completed_text_history=True,
    )


@pytest.mark.asyncio
async def test_prior_completed_messages_and_one_use_scope(bound):
    request, _, _, _, read = bound
    with scope(bound):
        selection = await history.select_run_history(request)
        assert "prior question" in selection.completed_history
        assert "prior answer" in selection.completed_history
        assert selection.current_run_checkpoint is None
        with pytest.raises(RuntimeError, match="already used"):
            await history.select_run_history(request)
    read.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", [False, None, 1, "true"])
async def test_missing_or_replayed_grant_never_reads(bound, grant):
    request, _, claim, _, read = bound
    claim["fresh_execution_grant"] = grant
    with scope(bound), pytest.raises(OperationRefused):
        await history.select_run_history(request)
    read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,value",
    [
        (("admission", "context", "tenant_id"), "foreign"),
        (("admission", "context", "subject_id"), "foreign"),
        (("admission", "context", "graph"), "foreign"),
        (
            ("admission", "conversation", "history_profile", "user_text_digest"),
            "c" * 64,
        ),
        (("checkpoint_selection", "run_id"), "foreign"),
        (("checkpoint_selection", "work_item_id"), "foreign"),
        (("checkpoint_selection", "admission_revision"), True),
        (("checkpoint_selection", "scope", "scope", "graph"), "foreign"),
        (("checkpoint_selection", "scope", "tenant"), "foreign"),
        (("checkpoint_selection", "scope", "identity_digest"), "a" * 64),
        (("checkpoint_selection", "scope", "identity_digest"), [10] * 31),
        (("checkpoint_selection", "completed_history"), "malformed"),
        (("checkpoint_selection", "checkpoints"), []),
    ],
)
async def test_foreign_or_malformed_selection_refuses(bound, path, value):
    request, _, _, view, _ = bound
    target = view
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with scope(bound), pytest.raises((OperationRefused, ValueError)):
        await history.select_run_history(request)


@pytest.mark.asyncio
async def test_current_presence_refuses_before_prior_decode(bound):
    request, _, _, view, _ = bound
    view["checkpoint_selection"].update(
        current_run_checkpoint="", completed_history=object()
    )
    with scope(bound), pytest.raises(OperationRefused):
        await history.select_run_history(request)


@pytest.mark.asyncio
async def test_cancelled_read_never_returns_selection(bound):
    request, _, _, _, read = bound
    entered = asyncio.Event()

    async def pending(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    read.side_effect = pending
    with scope(bound):
        running = asyncio.create_task(history.select_run_history(request))
        await entered.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert running.cancelled()


@pytest.mark.asyncio
async def test_provider_lifecycle_clears_binding():
    harness_host.register_harness_host()
    try:
        assert host_ports._RUN_CHECKPOINTS is history.select_run_history
    finally:
        harness_host.unregister_harness_host()
    assert host_ports._RUN_CHECKPOINTS is None


def test_utf8_and_escaped_envelope_bound():
    with pytest.raises(OperationRefused):
        history.completed_checkpoint("run", "question", "\x00" * 12000)
    value = history.completed_checkpoint("run", 'héllo " /\n', "答え")
    content = json.dumps(
        {
            "version": 1,
            "phase": "completed",
            "run_id": "run",
            "messages": [
                {"role": "user", "text": 'héllo " /\n'},
                {"role": "assistant", "text": "答え"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert value["content_digest"] == hashlib.sha256(content.encode()).hexdigest()


@pytest.mark.parametrize("event_type", ["graph_start", "graph_complete", "tool_call"])
def test_private_profile_trace_does_not_persist_transcript(
    bound, monkeypatch, event_type
):
    _, _, _, _, _ = bound
    rows = []
    monkeypatch.setattr(
        agent_runs,
        "_engine",
        lambda: SimpleNamespace(create_node=lambda *args: rows.append(args)),
    )
    with scope(bound):
        agent_runs.record_run_event(
            {
                "run_id": "run",
                "type": event_type,
                "step": 1,
                "status": "completed",
                "output": "PRIVATE ASSISTANT",
                "task": "PRIVATE USER",
                "nested": {"transcript": "PRIVATE NESTED"},
                "event_id": "PRIVATE ID",
                "sequence": 1,
            }
        )
    assert len(rows) == 1
    assert "PRIVATE" not in json.dumps(rows)
    payload = json.loads(rows[0][2]["payload"])
    assert payload["content_omitted"] is True
    assert payload["status"] == "completed"


def test_cross_language_completed_text_digest():
    value = history.completed_checkpoint("r/1", 'héllo "quote"\n/', "紫 yes\\end")
    assert (
        value["content_digest"]
        == "5fe52081485464c774065089358c1881dfdc2ff6beabfeba40023e5b7c7ea563"
    )


@pytest.mark.asyncio
async def test_delayed_copied_context_drops_event_after_scope_exit(bound, monkeypatch):
    from contextvars import copy_context

    rows = []
    monkeypatch.setattr(
        agent_runs,
        "_engine",
        lambda: SimpleNamespace(create_node=lambda *args: rows.append(args)),
    )
    with scope(bound):
        copied = copy_context()
    await asyncio.to_thread(
        copied.run,
        agent_runs.record_run_event,
        {"run_id": "run", "type": "graph_complete", "output": "PRIVATE"},
    )
    assert rows == []


@pytest.mark.asyncio
async def test_child_task_delayed_past_private_scope_cannot_persist(bound, monkeypatch):
    rows = []
    ready = asyncio.Event()
    monkeypatch.setattr(
        agent_runs,
        "_engine",
        lambda: SimpleNamespace(create_node=lambda *args: rows.append(args)),
    )

    async def delayed():
        await ready.wait()
        agent_runs.record_run_event(
            {"run_id": "run", "type": "graph_complete", "output": "PRIVATE"}
        )

    with scope(bound):
        child = asyncio.create_task(delayed())
    ready.set()
    await child
    assert rows == []


@pytest.mark.asyncio
async def test_context_free_worker_conservatively_redacts_after_private_scope(
    bound, monkeypatch
):
    rows = []
    monkeypatch.setattr(
        agent_runs,
        "_engine",
        lambda: SimpleNamespace(create_node=lambda *args: rows.append(args)),
    )
    with scope(bound):
        pass
    await asyncio.get_running_loop().run_in_executor(
        None,
        agent_runs.record_run_event,
        {"run_id": "run", "type": "graph_complete", "output": "PRIVATE"},
    )
    assert len(rows) == 1
    assert "PRIVATE" not in json.dumps(rows)
    assert json.loads(rows[0][2]["payload"])["content_omitted"] is True


def test_explicit_disabled_legacy_trace_keeps_original_body(monkeypatch):
    from arbora.core.settings import runtime_config

    monkeypatch.setattr(agent_runs, "_PRIVATE_TRACE_POLICY", False)
    monkeypatch.setattr(runtime_config, "completed_text_history", False)
    rows = []
    monkeypatch.setattr(
        agent_runs,
        "_engine",
        lambda: SimpleNamespace(create_node=lambda *args: rows.append(args)),
    )
    agent_runs.record_run_event(
        {"run_id": "legacy", "type": "graph_complete", "output": "legacy reply"}
    )
    assert json.loads(rows[0][2]["payload"])["output"] == "legacy reply"


@pytest.mark.asyncio
async def test_first_profile_turn_has_authoritative_absence(bound):
    request, _, _, view, _ = bound
    view["admission"]["conversation"].update(
        expected_revision=0, predecessor_run_id=None
    )
    view["checkpoint_selection"].update(
        admission_revision=1, completed_history=None, checkpoints=[]
    )
    with scope(bound):
        selection = await history.select_run_history(request)
    assert selection.completed_history is None
    assert selection.current_run_checkpoint is None


@pytest.mark.asyncio
async def test_wrong_run_and_revoked_copied_scope_refuse(bound):
    from contextvars import copy_context

    request, _, _, _, read = bound
    with scope(bound):
        with pytest.raises(RuntimeError, match="identity does not match"):
            await history.select_run_history(SimpleNamespace(run_id="wrong"))
        copied = copy_context()
    with pytest.raises(RuntimeError, match="scope is closed"):
        await asyncio.create_task(history.select_run_history(request), context=copied)
    read.assert_not_awaited()


def test_private_completed_reply_matches_native_receipt(bound):
    _, owner, _, view, _ = bound
    content = json.dumps(
        {
            "version": 1,
            "phase": "completed",
            "run_id": "run",
            "messages": [
                {"role": "user", "text": "next"},
                {"role": "assistant", "text": "private answer"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    view["completed_checkpoint"] = content
    view["work_item"]["status"] = "succeeded"
    view["run_completion"] = {
        "payload": {
            "tenant": "tenant",
            "work_item_id": "work",
            "outcome": "succeeded",
            "completion": {"run_id": "run", "schema_version": 1},
            "completed_checkpoint": {
                "document_id": "inline-checkpoint:work",
                "revision": 1,
                "content_digest": hashlib.sha256(content.encode()).hexdigest(),
            },
        }
    }
    task = SimpleNamespace(id="work", context_id="context")
    item = {"metadata": {"a2a_run_id": "run"}, "status": "succeeded"}
    artifacts = history.completed_reply(
        view, session=owner.session, task=task, item=item, owner_graph="private"
    )
    assert artifacts[0].parts[0].text == "private answer"
    from copy import deepcopy

    malformed = [
        ("completed_checkpoint", "not-json"),
        ("completed_checkpoint", "\ud800"),
        ("completed_checkpoint", content.replace("private answer", "\\ud800")),
        ("admission.context", None),
        ("admission.context", []),
        ("run_completion.payload.completion", None),
        ("run_completion.payload.completion", []),
        ("run_completion.payload.completion.schema_version", True),
        ("run_completion.payload.completed_checkpoint.revision", True),
        ("run_completion.payload.completed_checkpoint.document_id", "foreign"),
        ("run_completion.payload.completed_checkpoint.content_digest", "c" * 64),
    ]
    for path, value in malformed:
        candidate = deepcopy(view)
        target = candidate
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
        with pytest.raises(OperationRefused):
            history.completed_reply(
                candidate,
                session=owner.session,
                task=task,
                item=item,
                owner_graph="private",
            )


@pytest.mark.asyncio
async def test_request_metadata_cannot_enable_private_history(bound):
    request, owner, claim, _, read = bound
    request.metadata = {"completed_text_history": True, "fresh_execution_grant": True}
    with agent_runs.native_run_scope(
        run_id=request.run_id,
        task_id="task",
        context_id=owner.context_id,
        work_item_id=owner.work_item_id,
        session=owner.session,
        claim=claim,
        owner_graph=owner.owner_graph,
        completed_text_history=False,
    ):
        selection = await history.select_run_history(request)
    assert selection.completed_history is None
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoked_caller_after_native_read_never_returns_history(bound):
    request, owner, _, view, read = bound

    def revoked():
        raise PermissionError("caller revoked")

    async def revoke(*args, **kwargs):
        owner.session.ensure_authority_current = revoked
        return view

    read.side_effect = revoke
    with scope(bound), pytest.raises(PermissionError, match="revoked"):
        await history.select_run_history(request)
