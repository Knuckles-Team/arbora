"""Source composition of the agreed producer shape, not native acceptance."""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from epistemic_graph.generated import models as native_models
from epistemic_graph.generated.models import ConversationTurnAdmission

from arbora import work_items
from arbora.a2a import native_history as history
from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.api.contracts.agent_control_contracts import AgentExecutionResult
from arbora.api.invoke.pipeline import OperationRefused
from tests.a2a.test_native_history_selection import bound as bound

pytestmark = pytest.mark.spec("ARBORA-A2A-R001.3.3")


class CandidateAdmission(ConversationTurnAdmission):
    """Explicit source fixture until the new native generated model qualifies."""

    history_profile: dict[str, str | int] | None = None


CandidateAdmission.model_rebuild(_types_namespace=vars(native_models))


@pytest.mark.asyncio
async def test_old_generated_model_refuses_before_read(bound, monkeypatch):
    from epistemic_graph.generated import models

    monkeypatch.delitem(
        ConversationTurnAdmission.model_fields, "history_profile", raising=False
    )
    monkeypatch.setattr(models, "ConversationTurnAdmission", ConversationTurnAdmission)
    _, owner, _, _, read = bound
    authority = SimpleNamespace(
        _session=lambda _: owner.session,
        hosted_invocation=object(),
        _context_exists=lambda *args: False,
        _owner_ref=lambda _: "owner",
    )
    with pytest.raises(OperationRefused) as error:
        await history.prepare_admission(
            authority,
            object(),
            SimpleNamespace(work_item_graph="private"),
            session=owner.session,
            run_id="run",
            context_id="context",
            request_digest="request",
            user_text="next",
            resume=False,
        )
    assert error.value.code == "UPGRADE_REQUIRED"
    read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_new_turn_uses_only_native_head_candidate(bound, monkeypatch, resume):
    from epistemic_graph.generated import models

    monkeypatch.setattr(models, "ConversationTurnAdmission", CandidateAdmission)
    _, owner, _, view, read = bound
    head = {
        "version": 1,
        "context_id": "context",
        "revision": 1,
        "run_id": "previous",
        "scope": view["checkpoint_selection"]["scope"],
    }
    read.side_effect = [None, head if resume else None]
    authority = SimpleNamespace(
        _session=lambda _: owner.session,
        hosted_invocation=object(),
        _context_exists=lambda *args: False,
        _owner_ref=lambda _: "owner",
    )
    candidate = await history.prepare_admission(
        authority,
        object(),
        SimpleNamespace(work_item_graph="private"),
        session=owner.session,
        run_id="run",
        context_id="context",
        request_digest="request",
        user_text="next",
        resume=resume,
    )
    assert candidate.expected_revision == (1 if resume else 0)
    assert candidate.predecessor_run_id == ("previous" if resume else None)
    assert candidate.history_profile == {
        "version": 1,
        "user_text_digest": hashlib.sha256(b"next").hexdigest(),
    }
    assert 'user_text"' not in candidate.model_dump_json()
    assert read.await_count == 2


@pytest.mark.asyncio
async def test_absent_continuation_head_never_creates_replacement(bound, monkeypatch):
    from epistemic_graph.generated import models

    monkeypatch.setattr(models, "ConversationTurnAdmission", CandidateAdmission)
    _, owner, _, _, read = bound
    read.side_effect = [None, None]
    authority = SimpleNamespace(
        _session=lambda _: owner.session,
        hosted_invocation=object(),
        _context_exists=lambda *args: False,
        _owner_ref=lambda _: "owner",
    )
    with pytest.raises(OperationRefused):
        await history.prepare_admission(
            authority,
            object(),
            SimpleNamespace(work_item_graph="private"),
            session=owner.session,
            run_id="run",
            context_id="context",
            request_digest="request",
            user_text="next",
            resume=True,
        )


@pytest.mark.asyncio
async def test_retry_reuses_original_admission_without_reading_new_head(
    bound, monkeypatch
):
    from epistemic_graph.generated import models

    monkeypatch.setattr(models, "ConversationTurnAdmission", CandidateAdmission)
    _, owner, _, view, read = bound
    view["admission"]["metadata"] = {"a2a_request_digest": "request"}
    view["admission"]["conversation"]["schema_version"] = "1"
    authority = SimpleNamespace(
        _session=lambda _: owner.session,
        hosted_invocation=object(),
        _context_exists=lambda *args: False,
        _owner_ref=lambda _: "owner",
    )
    candidate = await history.prepare_admission(
        authority,
        object(),
        SimpleNamespace(work_item_graph="private"),
        session=owner.session,
        run_id="run",
        context_id="context",
        request_digest="request",
        user_text="next",
        resume=True,
    )
    assert candidate.expected_revision == 1
    assert candidate.predecessor_run_id == "previous"
    read.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", [None, False, 1, "true"])
async def test_profile_refuses_nonfresh_claim_before_model(monkeypatch, grant):
    authority = WorkItemA2AAuthority(lambda: None, completed_text_history=True)
    execute = AsyncMock()
    monkeypatch.setattr(authority, "_execute_claimed_run", execute)
    monkeypatch.setattr(
        work_items, "claim_specific", lambda *a, **k: {"fresh_execution_grant": grant}
    )
    with pytest.raises(OperationRefused):
        await authority._run(
            object(), item_id="work", session=object(), agent_name="helper", task="next"
        )
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_own_pair_only_after_successful_execution(monkeypatch):
    authority = WorkItemA2AAuthority(
        lambda: None, completed_text_history=True, task_plan_config=object()
    )
    execute = AsyncMock(
        return_value=AgentExecutionResult(run_id="run", output="answer")
    )
    monkeypatch.setattr(authority, "_execute_claimed_run", execute)
    monkeypatch.setattr(
        work_items, "claim_specific", lambda *a, **k: {"fresh_execution_grant": True}
    )
    commits = []
    monkeypatch.setattr(
        work_items, "commit_result", lambda *a, **k: commits.append(k) or "committed"
    )
    await authority._run(
        object(),
        item_id="work",
        session=object(),
        agent_name="helper",
        task="next",
        run_id="run",
    )
    assert commits[0]["outcome"] == "succeeded"
    assert commits[0]["run_completion"][
        "completed_checkpoint"
    ] == history.completed_checkpoint("run", "next", "answer")
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_keeps_profile_prompt_out_of_public_submission(
    bound, monkeypatch
):
    from arbora.a2a.models import A2AMessage, A2ARouteDecision, A2ATextPart
    from arbora.mcp_server.task_plan import TaskPlanConfig

    _, owner, _, _, _ = bound
    engine = SimpleNamespace(work_item_graph="private")
    authority = WorkItemA2AAuthority(
        lambda: engine,
        task_plan_config=TaskPlanConfig({}, {}, 1, 2),
        hosted_invocation=object(),
        completed_text_history=True,
    )
    monkeypatch.setattr(authority, "_session", lambda _: owner.session)
    prompt = "PRIVATE PROMPT SENTINEL"
    candidate = CandidateAdmission.model_validate_json(
        '{"schema_version":"1","context_id":"context","expected_revision":0,'
        '"predecessor_run_id":null,"history_profile":{"version":1,"user_text_digest":"'
        + hashlib.sha256(prompt.encode()).hexdigest()
        + '"}}',
        strict=True,
    )
    prepare = AsyncMock(return_value=candidate)
    monkeypatch.setattr(history, "prepare_admission", prepare)
    submissions = []
    item_id = "work-item:" + "a" * 64

    def submit(engine, **kwargs):
        submissions.append(kwargs)
        return item_id, True

    monkeypatch.setattr(work_items, "submit_work_item_atomic", submit)
    monkeypatch.setattr(
        work_items,
        "get_work_item",
        lambda *a: {
            "work_item_id": item_id,
            "kind": "orchestrator_task",
            "status": "running",
            "metadata": submissions[0]["metadata"],
        },
    )
    execute = AsyncMock(return_value=None)
    monkeypatch.setattr(authority, "_run", execute)
    monkeypatch.setattr(
        authority, "_project_dispatch_task", AsyncMock(return_value=object())
    )
    await authority.dispatch(
        message=A2AMessage(
            role="user", message_id="message", parts=[A2ATextPart(text=prompt)]
        ),
        idempotency_key="key",
        decision=A2ARouteDecision(agent_name="helper", selection_mode="explicit"),
    )
    assert submissions[0]["description"] == "Completed-text conversation turn"
    assert prompt not in str(submissions[0])
    assert prepare.await_args.kwargs["user_text"] == prompt
    assert execute.await_args.kwargs["task"] == prompt


@pytest.fixture
def browser_context(bound, monkeypatch, request):
    from unittest.mock import Mock

    from arbora.a2a.models import A2AMessage, A2ATextPart

    monkeypatch.setattr(native_models, "ConversationTurnAdmission", CandidateAdmission)
    _, owner, _, view, read = bound
    rows = Mock(return_value={"items": [], "next_cursor": None})
    engine = SimpleNamespace(
        work_item_graph="private", work_items=SimpleNamespace(list=rows)
    )
    authority = WorkItemA2AAuthority(
        lambda: engine, completed_text_history=True, hosted_invocation=object()
    )
    monkeypatch.setattr(authority, "_session", lambda _: owner.session)
    message = A2AMessage(
        role="user",
        message_id="first",
        context_id=getattr(request, "param", "webui:/V1StGXR8_Z5jdHi6B-myT"),
        parts=[A2ATextPart(text="first")],
    )
    context = authority._resolve_context_id(
        owner.session, message, "first-run", authority._owner_ref(owner.session)
    )
    head = {
        "version": 1,
        "context_id": context,
        "revision": 1,
        "run_id": "first-run",
        "scope": view["checkpoint_selection"]["scope"],
    }
    return authority, engine, owner.session, message, context, head, read, rows


async def browser_candidate(fixture, *, run="next-run", resume=False):
    authority, engine, owner, _, context, _, _, _ = fixture
    return await history.prepare_admission(
        authority,
        engine,
        engine,
        session=owner,
        run_id=run,
        context_id=context,
        request_digest=run,
        user_text="next",
        resume=resume,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "browser_context", ["webui:c1", "webui:/V1StGXR8_Z5jdHi6B-myT"], indirect=True
)
async def test_overstory_repeated_label_uses_native_head(browser_context):
    authority, _, owner, message, context, head, read, rows = browser_context
    read.side_effect = [None, None]
    first = await browser_candidate(browser_context, run="first-run")
    assert first.expected_revision == 0 and first.predecessor_run_id is None
    second_message = message.model_copy(update={"message_id": "second"})
    assert (
        authority._resolve_context_id(
            owner, second_message, "next-run", authority._owner_ref(owner)
        )
        == context
    )
    read.side_effect = [None, head]
    second = await browser_candidate(browser_context)
    assert second.expected_revision == 1
    assert second.predecessor_run_id == "first-run"
    rows.assert_called_once()
    assert rows.call_args.kwargs["metadata_match"]["a2a_context_id"] == context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("context_id", "foreign-context"), ("scope", {"tenant": "foreign"})]
)
async def test_label_refuses_foreign_native_head(browser_context, field, value):
    *_, head, read, rows = browser_context
    head[field] = value
    read.side_effect = [None, head]
    with pytest.raises(OperationRefused):
        await browser_candidate(browser_context)
    rows.assert_not_called()


@pytest.mark.asyncio
async def test_label_with_legacy_membership_and_no_native_head_refuses(browser_context):
    authority, _, owner, _, context, _, read, rows = browser_context
    rows.return_value = {
        "items": [
            {
                "kind": "orchestrator_task",
                "metadata": {
                    "a2a_schema": "arbora-a2a-unary-v1",
                    "a2a_context_id": context,
                    "a2a_owner_ref": authority._owner_ref(owner),
                },
            }
        ],
        "next_cursor": None,
    }
    read.side_effect = [None, None]
    with pytest.raises(OperationRefused):
        await browser_candidate(browser_context)
    rows.assert_called_once()


@pytest.mark.asyncio
async def test_canonical_locator_without_native_head_still_refuses(browser_context):
    *_, read, rows = browser_context
    read.side_effect = [None, None]
    with pytest.raises(OperationRefused):
        await browser_candidate(browser_context, resume=True)
    rows.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["outage", "malformed", "cycle", "foreign"])
async def test_label_membership_lookup_refuses_uncertainty(browser_context, failure):
    from arbora.a2a.authority import A2AContextUnavailable

    _, _, _, _, context, _, read, rows = browser_context
    read.side_effect = [None, None]
    if failure == "outage":
        rows.side_effect = RuntimeError("private backend details")
    elif failure == "malformed":
        rows.return_value = {"items": None}
    elif failure == "cycle":
        rows.return_value = {"items": [], "next_cursor": "same"}
    else:
        rows.return_value = {
            "items": [
                {
                    "kind": "orchestrator_task",
                    "metadata": {
                        "a2a_schema": "arbora-a2a-unary-v1",
                        "a2a_context_id": context,
                        "a2a_owner_ref": "foreign",
                    },
                }
            ]
        }
    with pytest.raises((A2AContextUnavailable, PermissionError)):
        await browser_candidate(browser_context)


@pytest.mark.asyncio
async def test_label_membership_searches_past_empty_page(browser_context):
    authority, _, owner, _, context, _, read, rows = browser_context
    read.side_effect = [None, None]
    rows.side_effect = [
        {"items": [], "next_cursor": "next"},
        {
            "items": [
                {
                    "kind": "orchestrator_task",
                    "metadata": {
                        "a2a_schema": "arbora-a2a-unary-v1",
                        "a2a_context_id": context,
                        "a2a_owner_ref": authority._owner_ref(owner),
                    },
                }
            ],
            "next_cursor": None,
        },
    ]
    with pytest.raises(OperationRefused):
        await browser_candidate(browser_context)
    assert rows.call_count == 2
    assert rows.call_args.kwargs["cursor"] == "next"
