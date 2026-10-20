"""Behaviour of the WorkItem seam against a fake engine ``work_items`` namespace."""

from __future__ import annotations

from typing import Any

import pytest

from arbora import work_items

_CLAIM = {
    "tenant": "t1",
    "lease_holder_ref": "worker-1",
    "lease_epoch": 3,
    "fencing_token": 7,
}


class FakeWorkItems:
    """Answers each engine WorkItem method with a canned result."""

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.calls: list[tuple[str, Any]] = []

    def _answer(self, name: str, payload: Any) -> Any:
        self.calls.append((name, payload))
        return self.answers[name]

    def submit(self, request: dict[str, Any]) -> Any:
        return self._answer("submit", request)

    def get(self, **kw: Any) -> Any:
        return self._answer("get", kw)

    def claim(self, request: dict[str, Any]) -> Any:
        return self._answer("claim", request)

    def renew(self, **kw: Any) -> Any:
        return self._answer("renew", kw)

    def commit_result(self, **kw: Any) -> Any:
        return self._answer("commit_result", kw)

    def defer(self, **kw: Any) -> Any:
        return self._answer("defer", kw)

    def cancel(self, **kw: Any) -> Any:
        return self._answer("cancel", kw)


class FakeEngine:
    def __init__(self, **answers: Any) -> None:
        self.work_items = FakeWorkItems(**answers)


class _Session:
    tenant = "t1"

    @staticmethod
    def engine_verified_context() -> dict[str, Any]:
        return {"tenant_id": "t1", "graph": "g1"}


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(work_items, "_session", _Session)
    return _Session


def test_submit_maps_labels_into_metadata(session: Any) -> None:
    engine = FakeEngine(submit={"work_item_id": "w1", "created": True})
    item_id, created = work_items.submit_work_item_atomic(
        engine,
        kind="k",
        idempotency_key="idem",
        tenant="t1",
        work_item_id="w1",
        queue="q",
        created_by="me",
        metadata={"a": 1},
    )
    ((_, request),) = engine.work_items.calls
    assert (item_id, created) == ("w1", True)
    assert request["metadata"] == {"queue": "q", "a": 1}
    assert request["provenance_refs"] == ["me"]
    assert request["context"]["tenant_id"] == "t1"
    assert len(request["command_digest"]) == 64


def test_claim_returns_none_when_not_granted(session: Any) -> None:
    engine = FakeEngine(claim={"claimed": False, "reason": "empty"})
    assert work_items.claim_specific(engine, "w1", token="o", lease_ttl_s=2) is None
    ((_, request),) = engine.work_items.calls
    assert (request["worker_ref"], request["lease_ms"]) == ("o", 2000)


def test_claim_carries_tenant_for_later_fenced_calls(session: Any) -> None:
    engine = FakeEngine(claim={"claimed": True, "lease_epoch": 1})
    claim = work_items.claim_specific(engine, "w1", token="o", lease_ttl_s=1)
    assert claim == {"claimed": True, "lease_epoch": 1, "tenant": "t1"}


@pytest.mark.parametrize(
    ("verb", "answer", "expected"),
    [
        ("heartbeat", {"renewed": True}, True),
        ("heartbeat", {"renewed": False}, False),
        ("commit_ok", {"status": "succeeded"}, "committed"),
        ("commit_noop", {"status": "noop"}, "noop"),
        ("commit_fenced", {"status": "fenced"}, "fenced"),
        ("defer", {"status": "deferred"}, True),
        ("defer", {"status": "fenced"}, False),
        ("cancel", {"status": "cancelled"}, True),
        ("cancel", {"status": "in_flight"}, False),
    ],
)
def test_fenced_transitions_map_engine_status(
    session: Any, verb: str, answer: dict[str, Any], expected: Any
) -> None:
    calls = {
        "heartbeat": (
            "renew",
            lambda e: work_items.heartbeat(e, "w1", _CLAIM, lease_ttl_s=1),
        ),
        "commit_ok": (
            "commit_result",
            lambda e: work_items.commit_result(e, "w1", _CLAIM, outcome="succeeded"),
        ),
        "commit_noop": (
            "commit_result",
            lambda e: work_items.commit_result(e, "w1", _CLAIM, outcome="failed"),
        ),
        "commit_fenced": (
            "commit_result",
            lambda e: work_items.commit_result(e, "w1", _CLAIM, outcome="failed"),
        ),
        "defer": (
            "defer",
            lambda e: work_items.defer_work_item(e, "w1", _CLAIM, next_retry_at=1.0),
        ),
        "cancel": (
            "cancel",
            lambda e: work_items.cancel_work_item(e, "w1", reason="r"),
        ),
    }
    method, call = calls[verb]
    engine = FakeEngine(**{method: answer})
    assert call(engine) == expected
    ((_, sent),) = engine.work_items.calls
    assert sent["tenant"] == "t1"
    if method != "cancel":
        assert (sent["lease_epoch"], sent["fencing_token"]) == (3, 7)


def test_get_reads_the_session_tenant(session: Any) -> None:
    engine = FakeEngine(get={"work_item_id": "w1"})
    assert work_items.get_work_item(engine, "w1") == {"work_item_id": "w1"}
    assert engine.work_items.calls == [("get", {"tenant": "t1", "work_item_id": "w1"})]


def test_refuses_a_tenant_other_than_the_session(session: Any) -> None:
    with pytest.raises(work_items.WorkItemBackendUnavailable):
        work_items.submit_work_item_atomic(
            FakeEngine(), kind="k", idempotency_key="i", tenant="other"
        )


def test_engine_without_work_items_is_refused(session: Any) -> None:
    with pytest.raises(work_items.NativeWorkItemRequired):
        work_items.get_work_item(object(), "w1")


@pytest.mark.parametrize("stream", [None, {"run_id": "run", "expected_index": 7}])
def test_terminal_stream_intent_forwarded_only_when_present(session, stream):
    engine = FakeEngine(commit_result={"status": "succeeded"})
    assert (
        work_items.commit_result(
            engine, "w1", _CLAIM, outcome="succeeded", terminal_stream=stream
        )
        == "committed"
    )
    payload = engine.work_items.calls[0][1]
    if stream is None:
        assert "terminal_stream" not in payload
    else:
        assert payload["terminal_stream"] == stream
        assert payload["terminal_stream"] is not stream
    assert payload["fencing_token"] == _CLAIM["fencing_token"]
