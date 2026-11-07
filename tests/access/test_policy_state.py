"""ARBORA-HOST-R039.3: the host's policy state is owned, never module-global."""

from __future__ import annotations

import pytest

import arbora.access.action_policy as ap
import arbora.access.approval as approval
from arbora.access.policy_state import PolicyState
from arbora.mcp_server.host_context import build_host_context

pytestmark = pytest.mark.spec("ARBORA-HOST-R039.3")


def test_two_policy_states_do_not_share_a_cache() -> None:
    engine = object()
    first, second = PolicyState(), PolicyState()

    gate = first.action_policies.for_engine(engine)

    assert first.action_policies.for_engine(engine) is gate
    assert second.action_policies.for_engine(engine) is not gate
    assert first.action_policies.for_engine() is not second.action_policies.for_engine()


def test_invalidate_drops_only_its_own_gates() -> None:
    engine = object()
    first, second = PolicyState(), PolicyState()
    kept = second.action_policies.for_engine(engine)
    dropped = first.action_policies.for_engine(engine)

    first.action_policies.invalidate()

    assert first.action_policies.for_engine(engine) is not dropped
    assert second.action_policies.for_engine(engine) is kept


def test_every_gate_reads_the_ports_of_its_own_state() -> None:
    seen: list[str] = []
    bound = ap.ActionPolicies(ap.ActionPolicyPorts(notifier=seen.append))

    bound.for_engine(object())._notify("to the bound notifier")
    ap.ActionPolicies().for_engine(object())._notify("logged only")

    assert seen == ["to the bound notifier"]


def test_the_host_context_owns_one_policy_state() -> None:
    first = build_host_context(lambda: None)
    second = build_host_context(lambda: None)

    assert isinstance(first.policy, PolicyState)
    assert first.policy.action_policies is not second.policy.action_policies


def test_no_module_policy_global_remains() -> None:
    for name in (
        "_ports",
        "_POLICY_CACHE",
        "_NO_ENGINE_POLICY",
        "_policy_cache_lock",
        "get_action_policy",
        "bind_action_policy_ports",
        "reset_action_policy_cache_for_tests",
    ):
        assert not hasattr(ap, name), name
    for name in ("_session_scope", "bind_approval_session", "decide_action_approval"):
        assert not hasattr(approval, name), name


class _ApprovalEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict, str]] = []

    def compare_and_set_node_fields(
        self, node_id, conditions, updates, *, idempotency_key
    ):
        self.calls.append((node_id, dict(conditions), dict(updates), idempotency_key))
        return True


def _verified_session():
    from arbora.security.actor_context import ActorContext, ActorType
    from arbora.security.session import GraphSession

    return GraphSession(
        actor=ActorContext(
            actor_id="owner",
            actor_type=ActorType.HUMAN,
            tenant_id="test",
            authenticated=True,
            roles=("kg:read", "kg:write"),
        ),
        tenant="test",
        scopes=frozenset({"kg:read", "kg:write"}),
    )


@pytest.mark.asyncio
async def test_served_approval_decides_only_under_a_verified_session(
    monkeypatch,
) -> None:
    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    from arbora.gateway.fleet import fleet_grant_approval
    from arbora.mcp_server import runtime
    from arbora.mcp_server.composition import install_gateway_application
    from arbora.security.session import use_session

    engine = _ApprovalEngine()
    monkeypatch.setattr(runtime, "_get_engine", lambda: engine)
    host = build_host_context(lambda: engine)
    install_gateway_application(fleet=host.fleet, approvals=host.policy.approvals)
    app = Starlette(
        routes=[Route("/fleet/approvals/grant", fleet_grant_approval, methods=["POST"])]
    )
    body = {"job_id": "action_approval:7", "decision": "approve"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://host"
    ) as client:
        refused = await client.post("/fleet/approvals/grant", json=body)
        assert refused.json()["status"] != "success"
        assert engine.calls == []

        with use_session(_verified_session()):
            decided = await client.post("/fleet/approvals/grant", json=body)

    assert decided.json() == {
        "status": "success",
        "result": {"approval_id": "action_approval:7", "decision": "approved"},
    }
    node_id, conditions, updates, _key = engine.calls[0]
    assert (node_id, conditions, updates["status"]) == (
        "action_approval:7",
        {"status": "pending"},
        "approved",
    )


def test_a_policy_state_without_a_session_scope_refuses_approvals() -> None:
    with pytest.raises(PermissionError):
        PolicyState().approvals.decide(object(), "action_approval:1", "approve")
