"""Action policy, approval decisions and the moved-capability refusal."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from arbora.access import action_policy as ap
from arbora.access import approval


@pytest.fixture(autouse=True)
def _fresh_policy(monkeypatch):
    monkeypatch.delenv("ACTION_POLICY_PATH", raising=False)
    monkeypatch.delenv("ACTION_IRREVERSIBILITY_AVERSION", raising=False)
    ap.reset_action_policy_cache_for_tests()
    ap.bind_action_policy_ports()
    approval.bind_approval_session(None)
    yield
    ap.reset_action_policy_cache_for_tests()
    ap.bind_action_policy_ports()
    approval.bind_approval_session(None)


def _request(kind: str, target: str = "svc", actor: str = ""):
    return ap.ActionRequest(kind=kind, target=target, source="test", actor_id=actor)


@pytest.mark.parametrize(
    ("kind", "tier"),
    [
        ("diagnose", ap.TIER_AUTO),
        ("observe", ap.TIER_AUTO),
        ("restart_service", ap.TIER_APPROVAL),
        ("an_unknown_kind", ap.TIER_APPROVAL),
    ],
)
def test_embedded_default_policy_tiers(kind: str, tier: str) -> None:
    assert ap.get_action_policy().classify(_request(kind)) == tier


def test_policy_file_rule_wins(tmp_path, monkeypatch) -> None:
    policy = tmp_path / "policy.yml"
    policy.write_text(
        "version: 1\ndefaults: {tier: approval_required}\n"
        "rules:\n  - {kind: restart_service, target: '*', tier: forbidden}\n"
    )
    monkeypatch.setenv("ACTION_POLICY_PATH", str(policy))
    ap.reset_action_policy_cache_for_tests()
    assert ap.get_action_policy().classify(_request("restart_service")) == (
        ap.TIER_FORBIDDEN
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("delete_volume", True), ("rollback_release", True), ("diagnose", False)],
)
def test_irreversible_kinds(kind: str, expected: bool) -> None:
    assert ap.is_irreversible(kind) is expected


@pytest.mark.parametrize(
    ("window", "hour", "inside"),
    [(None, 3, True), ("02:00-04:00", 3, True), ("02:00-04:00", 5, False)],
)
def test_maintenance_window(window: str | None, hour: int, inside: bool) -> None:
    assert ap.in_maintenance_window(window, now=hour * 3600.0) is inside


def test_unbound_trust_ramp_never_widens_scope() -> None:
    policy = ap.get_action_policy()
    assert policy._graduates(_request("restart_service", actor="a")) is False


def test_notifier_receives_message_and_failure_is_contained() -> None:
    seen: list[str] = []
    ap.bind_action_policy_ports(notifier=seen.append)
    ap.get_action_policy()._notify("hello")
    assert seen == ["hello"]

    def broken(_message: str) -> None:
        raise RuntimeError("down")

    ap.bind_action_policy_ports(notifier=broken)
    ap.get_action_policy()._notify("still fine")


def test_policy_cache_is_per_engine() -> None:
    engine = object()
    assert ap.get_action_policy(engine) is ap.get_action_policy(engine)
    assert ap.get_action_policy(engine) is not ap.get_action_policy(object())


class _Graph:
    def __init__(self, applied: bool) -> None:
        self.applied = applied
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def compare_and_set_node_fields(self, node_id, expected, update):
        self.calls.append((node_id, expected, update))
        return self.applied


def _bind(graph: _Graph) -> list[str]:
    scopes: list[str] = []

    @contextmanager
    def scope(_engine: Any, required: str):
        scopes.append(required)
        yield graph

    approval.bind_approval_session(scope)
    return scopes


@pytest.mark.parametrize(
    ("decision", "status"),
    [("approve", "approved"), ("Approved", "approved"), ("reject", "denied")],
)
def test_approval_decision_writes_through_the_session(decision, status) -> None:
    graph = _Graph(applied=True)
    scopes = _bind(graph)
    result = approval.decide_action_approval(None, "action_approval:1", decision)
    assert result == {"approval_id": "action_approval:1", "decision": status}
    assert scopes == ["kg:write"]
    assert graph.calls[0][1] == {"status": "pending"}


@pytest.mark.parametrize(
    ("approval_id", "decision", "bound", "applied", "error"),
    [
        ("other:1", "approve", True, True, ValueError),
        ("action_approval:1", "maybe", True, True, ValueError),
        ("action_approval:1", "approve", False, True, PermissionError),
        ("action_approval:1", "approve", True, False, LookupError),
    ],
)
def test_approval_decision_refusals(approval_id, decision, bound, applied, error):
    if bound:
        _bind(_Graph(applied=applied))
    with pytest.raises(error):
        approval.decide_action_approval(None, approval_id, decision)
