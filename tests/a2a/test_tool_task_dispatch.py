"""Native seam contract tests; these do not prove served task execution."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.a2a import tool_task_dispatch
from arbora.a2a.tool_task import WorkItemLeaseLost
from arbora.a2a.tool_task_dispatch import EgWorkItemFence
from arbora.api.contracts.session import use_session
from arbora.security.actor_context import CredentialLease
from tests._eg_fakes import service_session
from tests.test_work_items import FakeEngine

pytestmark = pytest.mark.spec("ARBORA-A2A-R002.2.2")


@pytest.fixture
def native(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, EgWorkItemFence]:
    session = SimpleNamespace(tenant="tenant-a")
    monkeypatch.setattr(tool_task_dispatch, "resolve_session", lambda **kwargs: session)
    monkeypatch.setattr(tool_task_dispatch.work_items, "_session", lambda: session)
    engine = FakeEngine(
        get={"work_item_id": "item-a", "input_ref": "request-a", "status": "ready"},
        renew={"renewed": True},
        commit_result={"status": "succeeded"},
    )
    fence = EgWorkItemFence(engine, tenant="tenant-a", lease_seconds=10)
    engine.work_items.answers["claim"] = {
        "claimed": True,
        "work_item_id": "item-a",
        "lease_holder_ref": fence._worker,
        "lease_epoch": 2,
        "fencing_token": 3,
        "attempt": 1,
        "payload_ref": "request-a",
    }
    return engine, fence


def test_native_fence_passes_exact_claim_through_transitions(native: Any) -> None:
    engine, fence = native
    row = fence.get("item-a")
    assert row["tenant"] == "tenant-a"
    assert row["payload_ref"] == "request-a"
    claim = fence.claim("item-a")
    fence.require_current("item-a", claim)
    fence.commit("item-a", claim, succeeded=True, ref="receipt-a")
    calls = dict(engine.work_items.calls)
    for method in ("renew", "commit_result"):
        assert calls[method]["lease_epoch"] == 2
        assert calls[method]["fencing_token"] == 3
        assert calls[method]["tenant"] == "tenant-a"
    assert calls["commit_result"]["result_ref"] == "receipt-a"
    assert calls["commit_result"]["retryable"] is False


@pytest.mark.parametrize("status", ["fenced", "noop", "failed", "missing"])
def test_unconfirmed_commit_never_allows_ack(native: Any, status: str) -> None:
    engine, fence = native
    claim = fence.claim("item-a")
    engine.work_items.answers["commit_result"] = {"status": status}
    with pytest.raises(WorkItemLeaseLost, match="commit refused"):
        fence.commit("item-a", claim, succeeded=True, ref="receipt-a")


def test_native_refused_renewal_stops_execution(native: Any) -> None:
    engine, fence = native
    claim = fence.claim("item-a")
    engine.work_items.answers["renew"] = {"renewed": False}
    with pytest.raises(WorkItemLeaseLost, match="renewal refused"):
        fence.require_current("item-a", claim)


@pytest.mark.parametrize("field", ["item_id", "tenant", "lease_holder_ref"])
def test_foreign_claim_never_reaches_native_transition(native: Any, field: str) -> None:
    engine, fence = native
    claim = json.loads(fence.claim("item-a"))
    target = claim if field == "item_id" else claim["claim"]
    target[field] = "foreign"
    before = len(engine.work_items.calls)
    with pytest.raises(WorkItemLeaseLost, match="invalid"):
        fence.require_current("item-a", json.dumps(claim))
    assert len(engine.work_items.calls) == before


def test_changed_tenant_never_reads_native_state(native: Any, monkeypatch: Any) -> None:
    engine, fence = native
    monkeypatch.setattr(
        tool_task_dispatch,
        "resolve_session",
        lambda **kwargs: SimpleNamespace(tenant="foreign"),
    )
    with pytest.raises(WorkItemLeaseLost, match="tenant changed"):
        fence.get("item-a")
    assert engine.work_items.calls == []


def test_denied_claim_and_missing_item_remain_unavailable(native: Any) -> None:
    engine, fence = native
    engine.work_items.answers.update(get=None, claim={"claimed": False})
    assert fence.get("item-a") is None
    assert fence.claim("item-a") is None


@pytest.mark.parametrize("status", ["leased", "running"])
def test_started_item_never_reclaims(native: Any, status: str) -> None:
    engine, fence = native
    engine.work_items.answers["get"]["status"] = status
    assert fence.claim("item-a") is None
    assert [name for name, _ in engine.work_items.calls] == ["get"]


@pytest.mark.parametrize("status", ["dead_letter", "cancelled"])
def test_uncertain_terminal_status_never_projects_ack(native: Any, status: str) -> None:
    engine, fence = native
    engine.work_items.answers["get"]["status"] = status
    with pytest.raises(WorkItemLeaseLost, match="reconciliation"):
        fence.get("item-a")


@pytest.mark.parametrize(
    "changes",
    [
        {"attempt": 2},
        {"attempt": None},
        {"attempt": True},
        {"work_item_id": "foreign"},
        {"payload_ref": "foreign"},
    ],
)
def test_claim_response_race_never_grants_execution(native: Any, changes: Any) -> None:
    engine, fence = native
    engine.work_items.answers["claim"].update(changes)
    assert fence.claim("item-a") is None


@pytest.mark.parametrize("refusal", ["expired", "scope_removed"])
def test_current_session_refuses_before_native_read(refusal: str) -> None:
    engine = FakeEngine()
    fence = EgWorkItemFence(engine, tenant="tenant-a")
    initial = service_session("tenant-a", "__control__")
    lease = CredentialLease(int(time.time()) + 60)
    session = replace(
        initial,
        actor=replace(initial.actor, credential_lease=lease),
        scopes=frozenset({"kg:write"}),
    )
    with use_session(session):
        if refusal == "expired":
            lease.renew(0)
            with pytest.raises(PermissionError):
                fence.get("item-a")
        else:
            with use_session(replace(session, scopes=frozenset({"kg:read"}))):
                with pytest.raises(PermissionError, match="kg:write"):
                    fence.get("item-a")
    assert engine.work_items.calls == []
