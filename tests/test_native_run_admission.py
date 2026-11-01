"""Admission recovery unit checks; these do not prove native lease execution."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from arbora import native_run_admission as recovery
from arbora.api.invoke.pipeline import OperationRefused

pytestmark = pytest.mark.spec("ARBORA-OPS-R001")


def request() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "run_id": "run",
        "work_item_id": None,
        "context": {
            "schema_version": "2",
            "agent_id": "a",
            "audience": "eg",
            "authentication_method": "oidc",
            "issued_at_ms": 100,
            "expires_at_ms": 200,
            "graph": "private-owner",
            "policy_version": "v1",
            "request_id": "request",
            "scopes": ["read", "write"],
            "subject_id": "owner",
            "tenant_id": "tenant",
            "trace_id": "trace",
        },
        "idempotency_key": "key",
        "command_digest": "digest",
        "kind": "agent",
        "priority": 0,
        "depends_on": [],
        "input_ref": "input",
        "policy_digest": "policy",
        "catalog_digest": "catalog",
        "model_digest": "model",
        "max_attempts": 1,
        "deadline_unix": None,
        "metadata": {"request_digest": "original"},
        "provenance_refs": [],
        "max_tenant_in_flight": 4096,
    }


def view() -> dict[str, Any]:
    return {
        "admission": request(),
        "work_item": {
            "work_item_id": "engine-issued",
            "kind": "agent",
            "input_ref": "input",
            "metadata": {"request_digest": "mutable-current"},
            "status": "succeeded",
            "updated_at_ms": 300,
            "version": 2,
        },
    }


class Namespace:
    def __init__(self, answers: list[Any], error: Exception | None = None) -> None:
        self.answers = answers
        self.error = error
        self.calls: list[str] = []

    def get_run(self, *, tenant: str, run_id: str) -> Any:
        assert isinstance(tenant, str) and isinstance(run_id, str)
        self.calls.append("lookup")
        value = self.answers.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def submit(self, value: Any) -> dict[str, Any]:
        self.calls.append("submit")
        if self.error is not None:
            raise self.error
        return {"work_item_id": "engine-issued", "created": True}


def test_resume_ignores_only_timestamps_and_current_metadata() -> None:
    fresh = request()
    fresh["context"].update(issued_at_ms=300, expires_at_ms=400)
    namespace = Namespace([view()])
    assert recovery.submit(namespace, fresh) == ("engine-issued", False)
    assert namespace.calls == ["lookup"]


@pytest.mark.parametrize("field", [k for k in request() if k != "context"])
def test_every_submit_field_is_compared(field: str) -> None:
    fresh = request()
    value = fresh[field]
    if field in {"schema_version", "work_item_id", "run_id"}:
        fresh[field] = "different"
    elif value is None:
        fresh[field] = 12.0
    elif isinstance(value, int):
        fresh[field] = value + 1
    elif isinstance(value, list):
        fresh[field] = ["different"]
    elif isinstance(value, dict):
        fresh[field] = {"request_digest": "different"}
    else:
        fresh[field] = "different"
    namespace = Namespace([view()])
    # Invalid schema versions also fail closed before any submit.
    with pytest.raises((ValueError, OperationRefused)):
        recovery.submit(namespace, fresh)
    assert "submit" not in namespace.calls


@pytest.mark.parametrize(
    "field",
    [k for k in request()["context"] if k not in {"issued_at_ms", "expires_at_ms"}],
)
def test_every_context_authority_field_is_compared(field: str) -> None:
    original = view()
    original["admission"]["context"][field] = (
        ["different"] if field == "scopes" else "different"
    )
    namespace = Namespace([original])
    with pytest.raises((ValueError, OperationRefused)):
        recovery.submit(namespace, request())
    assert namespace.calls == ["lookup"]


@pytest.mark.parametrize("reason", ["missing", "same-tenant-foreign"])
def test_absent_owner_view_submits_once(reason: str) -> None:
    namespace = Namespace([None])
    assert recovery.submit(namespace, request()) == ("engine-issued", True)
    assert namespace.calls == ["lookup", "submit"]


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("lost"),
        ConnectionError("lost"),
        SimpleNamespace(code="IDEMPOTENCY_CONFLICT"),
    ],
)
def test_ambiguous_submit_recovers_matching_winner(error: Any) -> None:
    if not isinstance(error, Exception):
        error = RuntimeError("race")
        error.code = "IDEMPOTENCY_CONFLICT"
    namespace = Namespace([None, view()], error)
    assert recovery.submit(namespace, request()) == ("engine-issued", False)
    assert namespace.calls == ["lookup", "submit", "lookup"]


def test_missing_after_lost_response_preserves_original_error() -> None:
    error = TimeoutError("lost")
    namespace = Namespace([None, None], error)
    with pytest.raises(TimeoutError) as caught:
        recovery.submit(namespace, request())
    assert caught.value is error
    assert namespace.calls == ["lookup", "submit", "lookup"]


def test_race_mismatch_refuses() -> None:
    winner = view()
    winner["admission"]["metadata"] = {"request_digest": "foreign-request"}
    namespace = Namespace([None, winner], TimeoutError("lost"))
    with pytest.raises(OperationRefused):
        recovery.submit(namespace, request())
    assert namespace.calls == ["lookup", "submit", "lookup"]


def test_legacy_unavailable_propagates_without_submit() -> None:
    from medulla.client import EngineResponseError

    error = EngineResponseError("RUN_ADMISSION_UNAVAILABLE")
    namespace = Namespace([error])
    with pytest.raises(RuntimeError) as caught:
        recovery.submit(namespace, request())
    assert caught.value is error
    assert namespace.calls == ["lookup"]


def test_missing_capability_never_submits() -> None:
    calls: list[Any] = []
    with pytest.raises(OperationRefused):
        recovery.submit(SimpleNamespace(submit=calls.append), request())
    assert not calls


def test_response_extra_authority_refused() -> None:
    value = deepcopy(view())
    value["work_item"]["fencing_token"] = 12
    namespace = Namespace([value])
    with pytest.raises(ValueError):
        recovery.submit(namespace, request())
    assert namespace.calls == ["lookup"]


def test_blocking_adapter_routes_typed_lookup() -> None:
    from arbora.mcp_server.served_engine import _BlockingWorkItems

    calls: list[Any] = []

    async def get_run(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return view()

    adapter = _BlockingWorkItems(SimpleNamespace(get_run=get_run))
    assert recovery.submit(adapter, request()) == ("engine-issued", False)
    assert calls == [{"tenant": "tenant", "run_id": "run"}]


def test_blocking_adapter_missing_capability_fails_before_submit() -> None:
    from arbora.mcp_server.served_engine import _BlockingWorkItems

    calls: list[Any] = []
    adapter = _BlockingWorkItems(SimpleNamespace(submit=calls.append))
    with pytest.raises(OperationRefused):
        recovery.submit(adapter, request())
    assert not calls


def test_public_native_submit_uses_immutable_admission(monkeypatch: Any) -> None:
    from arbora import work_items

    namespace = Namespace([None])
    session = SimpleNamespace(tenant="tenant")
    monkeypatch.setattr(work_items, "_session", lambda: session)
    monkeypatch.setattr(work_items, "session_context", lambda _: {})
    monkeypatch.setattr(
        work_items, "_typed_request_context", lambda *args: request()["context"]
    )
    engine = SimpleNamespace(work_items=namespace, work_item_graph="private-owner")
    assert work_items.submit_work_item_atomic(
        engine, kind="agent", idempotency_key="key", run_id="run"
    ) == ("engine-issued", True)
    assert namespace.calls == ["lookup", "submit"]


def test_placement_epoch_change_refuses() -> None:
    fresh = request()
    fresh["context"]["placement_epoch"] = 9
    namespace = Namespace([view()])
    with pytest.raises(OperationRefused):
        recovery.submit(namespace, fresh)
    assert namespace.calls == ["lookup"]


def test_nonambiguous_refusal_is_not_retried() -> None:
    from medulla.client import EngineResponseError

    error = EngineResponseError("FORBIDDEN")
    namespace = Namespace([None], error)
    with pytest.raises(EngineResponseError) as caught:
        recovery.submit(namespace, request())
    assert caught.value is error
    assert namespace.calls == ["lookup", "submit"]
