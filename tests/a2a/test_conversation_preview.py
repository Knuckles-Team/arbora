"""Internal projection qualification, not served fork acceptance."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arbora.a2a.authority import A2AContextUnavailable, WorkItemA2AAuthority
from arbora.a2a.models import A2AArtifact, A2ATextPart
from tests.a2a.test_run_usage import record

pytestmark = pytest.mark.spec("ARBORA-A2A-R001.3.2")


def turn(run_id, revision, predecessor):
    value = record()
    admission = value["admission"]
    admission["run_id"] = run_id
    admission["metadata"].update(a2a_run_id=run_id, description=f"input {run_id}")
    admission["conversation"] = {
        "schema_version": "1",
        "context_id": admission["metadata"]["a2a_context_id"],
        "expected_revision": revision,
        "predecessor_run_id": predecessor,
    }
    value["work_item"]["work_item_id"] = f"work-item:{run_id}"
    value["run_completion"]["payload"]["work_item_id"] = f"work-item:{run_id}"
    value["run_completion"]["payload"]["completion"]["run_id"] = run_id
    return value


@pytest.fixture
def history(monkeypatch):
    records = {"first": turn("first", 0, None), "second": turn("second", 1, "first")}
    lookup = AsyncMock(side_effect=lambda **kw: deepcopy(records.get(kw["run_id"])))
    engine = SimpleNamespace(
        work_item_graph="private-owner",
        graph_compute=SimpleNamespace(
            for_graph=lambda graph: SimpleNamespace(
                async_client=SimpleNamespace(work_items=SimpleNamespace(get_run=lookup))
            )
        ),
    )
    authority = WorkItemA2AAuthority(lambda: engine)
    session = SimpleNamespace(tenant="tenant")
    monkeypatch.setattr(authority, "_session", lambda scope: session)
    monkeypatch.setattr(authority, "_owner_ref", lambda session: "owner")
    monkeypatch.setattr(
        authority,
        "_persisted_artifacts",
        lambda task_id, **kw: [
            A2AArtifact(artifact_id="reply", parts=[A2ATextPart(text="answer")])
        ],
    )
    return authority, records, lookup


@pytest.mark.asyncio
async def test_preview_reads_ordered_immutable_inputs_and_binds_digest(history):
    authority, records, lookup = history
    preview = await authority.preview_conversation(run_id="second", expected_revision=1)
    assert preview.selected.user_text == "input second"
    assert preview.selected.assistant_text is None
    assert [entry.user_text for entry in preview.completed_prefix] == ["input first"]
    assert preview.completed_prefix[0].assistant_text == "answer"
    assert [call.kwargs["run_id"] for call in lookup.await_args_list] == [
        "second",
        "first",
    ]
    records["first"]["work_item"]["metadata"]["description"] = "mutable forgery"
    assert (
        await authority.preview_conversation(run_id="second", expected_revision=1)
    ) == preview
    records["first"]["admission"]["metadata"]["description"] = "changed input"
    assert (
        await authority.preview_conversation(run_id="second", expected_revision=1)
    ).digest != preview.digest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "owner",
        "tenant",
        "graph",
        "legacy",
        "revision",
        "incomplete",
        "oversize",
        "context",
    ],
)
async def test_unproven_prefix_refuses(history, mutation):
    authority, records, _ = history
    first = records["first"]
    if mutation == "owner":
        first["admission"]["metadata"]["a2a_owner_ref"] = "foreign"
    elif mutation == "tenant":
        first["admission"]["context"]["tenant_id"] = "foreign"
    elif mutation == "graph":
        first["admission"]["context"]["graph"] = "foreign"
    elif mutation == "legacy":
        first["admission"].pop("conversation")
    elif mutation == "revision":
        records["second"]["admission"]["conversation"]["expected_revision"] = 2
    elif mutation == "incomplete":
        first["work_item"]["status"] = "running"
    elif mutation == "oversize":
        first["admission"]["metadata"]["description"] = "é" * 33_000
    else:
        first["admission"]["conversation"]["context_id"] = "foreign"
    with pytest.raises((PermissionError, A2AContextUnavailable)):
        await authority.preview_conversation(run_id="second", expected_revision=1)


@pytest.mark.asyncio
async def test_missing_reply_refuses_not_empty_history(history, monkeypatch):
    authority, _, _ = history
    monkeypatch.setattr(authority, "_persisted_artifacts", lambda *args, **kw: None)
    with pytest.raises(A2AContextUnavailable):
        await authority.preview_conversation(run_id="second", expected_revision=1)


@pytest.mark.asyncio
async def test_selected_first_turn_needs_no_prior_completion(history):
    authority, records, lookup = history
    records["first"]["run_completion"] = None
    preview = await authority.preview_conversation(run_id="first", expected_revision=0)
    assert preview.completed_prefix == ()
    assert preview.selected.assistant_text is None
    assert lookup.await_count == 1


@pytest.mark.asyncio
async def test_later_appends_do_not_change_selected_prefix(history):
    authority, records, _ = history
    before = await authority.preview_conversation(run_id="second", expected_revision=1)
    records["third"] = turn("third", 2, "second")
    assert (
        await authority.preview_conversation(run_id="second", expected_revision=1)
        == before
    )


@pytest.mark.asyncio
async def test_changed_displayed_reply_changes_digest(history, monkeypatch):
    authority, _, _ = history
    before = await authority.preview_conversation(run_id="second", expected_revision=1)
    monkeypatch.setattr(
        authority,
        "_persisted_artifacts",
        lambda *args, **kw: [
            A2AArtifact(artifact_id="reply", parts=[A2ATextPart(text="changed answer")])
        ],
    )
    after = await authority.preview_conversation(run_id="second", expected_revision=1)
    assert before.digest != after.digest


@pytest.mark.asyncio
async def test_caller_change_refuses_before_reply_read(history, monkeypatch):
    authority, _, lookup = history
    sessions = iter(
        [SimpleNamespace(tenant="tenant"), SimpleNamespace(tenant="foreign")]
    )
    monkeypatch.setattr(authority, "_session", lambda scope: next(sessions))
    monkeypatch.setattr(
        authority, "_persisted_artifacts", lambda *args, **kw: pytest.fail("reply read")
    )
    with pytest.raises(PermissionError):
        await authority.preview_conversation(run_id="second", expected_revision=1)
    assert lookup.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [-1, True, "1"])
async def test_invalid_selection_refuses_before_read(history, revision):
    authority, _, lookup = history
    with pytest.raises(ValueError):
        await authority.preview_conversation(
            run_id="second", expected_revision=revision
        )
    lookup.assert_not_awaited()
