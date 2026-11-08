"""Unique-id records are created through EG ``CreateNodeIfAbsent``.

On the live 0.1.5 engine ``AddNode`` was refused (``INTERNAL: unclassified
engine refusal``) while ``CreateNodeIfAbsent`` was admitted, so the
action-policy decision write failed and onboarding refused every pack import.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.unique_records import create_record, has_record_writer


class _CreateOnlyEngine:
    """The served engine's shape: ``create_node`` creates; ``add_node`` must not run."""

    def __init__(self) -> None:
        self.created: dict[str, tuple[str, dict[str, Any]]] = {}

    def create_node(self, node_id: str, label: str, properties: Any) -> bool:
        if node_id in self.created:
            return False
        self.created[node_id] = (label, dict(properties))
        return True

    def add_node(self, *args: Any, **kwargs: Any) -> str:  # pragma: no cover
        raise AssertionError("a unique-id record must not upsert through AddNode")

    def query_cypher(self, query: str, params: Any = None) -> list[Any]:
        return []


@pytest.mark.spec("ARBORA-HOST-R033")
def test_action_policy_decisions_and_approvals_are_created() -> None:
    from arbora.access.action_policy import (
        DECISION_ALLOW,
        TIER_AUTO_NOTIFY,
        ActionDecision,
        ActionPolicy,
        ActionRequest,
    )

    engine = _CreateOnlyEngine()
    policy = ActionPolicy(engine=engine)
    request = ActionRequest(kind="connector_pack_import", target="mattermost-mcp")
    audit_id = policy._audit(
        ActionDecision(
            decision=DECISION_ALLOW,
            tier=TIER_AUTO_NOTIFY,
            request=request,
            reason="rule allows",
            rule_origin="test",
        )
    )
    approval_id = policy.queue_approval(request, reason="held")
    assert audit_id and approval_id
    assert engine.created[audit_id][0] == "ActionDecision"
    assert engine.created[audit_id][1]["kind"] == "connector_pack_import"
    assert engine.created[approval_id][0] == "ActionApproval"
    assert engine.created[approval_id][1]["status"] == "pending"


@pytest.mark.spec("ARBORA-HOST-R033")
def test_a_repeated_create_reports_the_existing_record() -> None:
    engine = _CreateOnlyEngine()
    assert create_record(engine, "fleetevent:1", "FleetEvent", {"a": 1}) is True
    assert create_record(engine, "fleetevent:1", "FleetEvent", {"a": 2}) is False
    assert engine.created["fleetevent:1"] == ("FleetEvent", {"a": 1})


@pytest.mark.spec("ARBORA-HOST-R033")
def test_a_refused_create_keeps_the_engine_refusal() -> None:
    class _Refusal(Exception):
        code = "INTERNAL"

    class _Engine:
        def create_node(self, *args: Any) -> bool:
            raise _Refusal("unclassified engine refusal")

    with pytest.raises(_Refusal):
        create_record(_Engine(), "x", "ErrorDetail", {})
    with pytest.raises(RuntimeError, match="no engine atomic create"):
        create_record(SimpleNamespace(), "x", "ErrorDetail", {})


@pytest.mark.spec("ARBORA-HOST-R033")
def test_the_served_engine_create_sends_create_node_if_absent(monkeypatch) -> None:
    from medulla.generated import graph as generated

    from arbora.mcp_server import served_engine as served

    sent: list[str] = []

    async def create(client: Any, params: Any, graph: Any, *, idempotency_key=None):
        sent.append(params["node_id"])
        return True

    async def add(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("AddNode must not be sent for a unique record")

    monkeypatch.setattr(generated, "send_create_node_if_absent", create)
    monkeypatch.setattr(generated, "send_add_node", add)
    engine = served.ServedEngine.__new__(served.ServedEngine)
    view = SimpleNamespace(graph_name="tenant__t__g")
    monkeypatch.setattr(engine, "_session_client", lambda: view, raising=False)
    assert create_record(engine, "action_decision:1", "ActionDecision", {}) is True
    assert sent == ["action_decision:1"]


@pytest.mark.spec("ARBORA-HOST-R033")
@pytest.mark.parametrize("create_binding", [None, False])
def test_add_only_binding_is_refused_without_overwriting(create_binding: Any) -> None:
    existing = {"status": "answered", "attempts": 3, "timestamp": "original"}
    records = {"inbox:1": dict(existing)}
    writes: list[str] = []

    def add(node_id: str, label: str, properties: Any) -> None:
        writes.append(node_id)
        records[node_id] = dict(properties)

    engine = SimpleNamespace(add_node=add, create_node=create_binding)
    assert has_record_writer(engine) is False
    with pytest.raises(RuntimeError, match="no engine atomic create"):
        create_record(engine, "inbox:1", "InboxMessage", {"status": "pending"})
    assert writes == []
    assert records == {"inbox:1": existing}


@pytest.mark.spec("ARBORA-HOST-R033")
def test_record_writer_requires_callable_create() -> None:
    assert has_record_writer(None) is False
    assert has_record_writer(SimpleNamespace()) is False
    assert has_record_writer(_CreateOnlyEngine()) is True
