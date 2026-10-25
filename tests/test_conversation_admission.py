"""Typed conversation handoff, not served edit-and-fork acceptance."""

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from epistemic_graph.generated.models import ConversationTurnAdmission

from arbora import native_run_admission, work_items
from arbora.api.invoke.pipeline import OperationRefused
from tests.test_native_run_admission import Namespace, request, view

pytestmark = pytest.mark.spec("ARBORA-A2A-R001.3.1")


def conversation(**changes: Any) -> ConversationTurnAdmission:
    return ConversationTurnAdmission.model_validate_json(
        json.dumps(
            {
                "schema_version": "1",
                "context_id": "branch",
                "expected_revision": 0,
                "predecessor_run_id": None,
                **changes,
            }
        ),
        strict=True,
    )


@pytest.fixture
def submission(monkeypatch: Any) -> Any:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        work_items, "_session", lambda: SimpleNamespace(tenant="tenant")
    )
    monkeypatch.setattr(work_items, "session_context", lambda _: {})
    monkeypatch.setattr(
        work_items, "_typed_request_context", lambda *args: request()["context"]
    )

    def submit(namespace: Any, body: dict[str, Any]) -> tuple[str, bool]:
        calls.append(body)
        return "engine-issued", True

    monkeypatch.setattr(native_run_admission, "submit", submit)
    engine = SimpleNamespace(work_items=object(), work_item_graph="private-owner")
    return engine, calls


def test_typed_conversation_is_bound_to_original_request(submission: Any) -> None:
    engine, calls = submission
    for turn in (None, conversation(), conversation(context_id="other")):
        work_items.submit_work_item_atomic(
            engine, kind="agent", idempotency_key="key", run_id="run", conversation=turn
        )
    assert "conversation" not in calls[0]
    assert calls[1]["conversation"] == conversation().model_dump(mode="json")
    assert len({body["command_digest"] for body in calls}) == 3
    assert "conversation" not in calls[1]["metadata"]


@pytest.mark.parametrize("run_id,turn", [(None, conversation()), ("run", {})])
def test_untyped_or_non_native_turn_refuses_before_submission(
    submission: Any, run_id: Any, turn: Any
) -> None:
    engine, calls = submission
    with pytest.raises((TypeError, ValueError)):
        work_items.submit_work_item_atomic(
            engine,
            kind="agent",
            idempotency_key="key",
            run_id=run_id,
            conversation=turn,
        )
    assert calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("context_id", "changed"),
        ("expected_revision", 1),
        ("predecessor_run_id", "previous"),
    ],
)
def test_conversation_change_conflicts_with_immutable_admission(
    field: str, value: Any
) -> None:
    original = view()
    original["admission"]["conversation"] = conversation().model_dump(mode="json")
    fresh = deepcopy(original["admission"])
    fresh["conversation"][field] = value
    namespace = Namespace([original])
    with pytest.raises(OperationRefused):
        native_run_admission.submit(namespace, fresh)
    assert namespace.calls == ["lookup"]


def test_lost_response_recovers_exact_conversation() -> None:
    original = view()
    original["admission"]["conversation"] = conversation().model_dump(mode="json")
    namespace = Namespace([None, original], TimeoutError("lost"))
    assert native_run_admission.submit(namespace, original["admission"]) == (
        "engine-issued",
        False,
    )
    assert namespace.calls == ["lookup", "submit", "lookup"]


def test_missing_generated_capability_refuses_without_submit(
    submission: Any, monkeypatch: Any
) -> None:
    from epistemic_graph.generated.models import SubmitWorkItemRequest

    engine, calls = submission
    monkeypatch.delitem(SubmitWorkItemRequest.model_fields, "conversation")
    with pytest.raises(OperationRefused) as error:
        work_items.submit_work_item_atomic(
            engine,
            kind="agent",
            idempotency_key="key",
            run_id="run",
            conversation=conversation(),
        )
    assert error.value.code == "UPGRADE_REQUIRED"
    assert not calls


def test_constructed_invalid_carrier_is_revalidated(submission: Any) -> None:
    engine, calls = submission
    invalid = conversation().model_copy(update={"expected_revision": -1})
    with pytest.raises(ValueError):
        work_items.submit_work_item_atomic(
            engine,
            kind="agent",
            idempotency_key="key",
            run_id="run",
            conversation=invalid,
        )
    assert not calls
