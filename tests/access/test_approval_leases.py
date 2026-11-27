"""Action approvals are engine ``action.approval`` control leases.

The engine refuses a generic node write of an ``ActionApproval`` row, so a held
action files its approval through ``IssueControlLease`` and every approval door
lists and decides that same lease (ARBORA-IDENTITY-R019.8, R019.9).
"""

from __future__ import annotations

from typing import Any

import pytest

from arbora.access import approval_leases
from arbora.access.action_policy import (
    DECISION_ALLOW,
    DECISION_QUEUE,
    DECISION_UNAVAILABLE,
    ActionPolicy,
    ActionRequest,
)
from tests._eg_fakes import SyncControlLeases


class _Engine:
    """The served engine's synchronous shape: audit rows plus control leases."""

    def __init__(self, leases: Any = None) -> None:
        self.created: dict[str, tuple[str, dict[str, Any]]] = {}
        if leases is not None:
            self.control_leases = leases

    def create_node(self, node_id: str, label: str, properties: Any) -> bool:
        if label in {"ActionApproval", "ControlLease", "Gap"}:
            raise RuntimeError(
                "native record authority required for a ControlLease, "
                "ActionApproval or Gap row"
            )
        if node_id in self.created:
            return False
        self.created[node_id] = (label, dict(properties))
        return True

    def query_cypher(self, query: str, params: Any = None) -> list[Any]:
        return []


def _held() -> ActionRequest:
    # restart_service is an approval_required tier in the shipped default policy.
    return ActionRequest(
        kind="restart_service", target="svc-a", params={"n": 1}, source="test"
    )


def _policy(engine: Any, monkeypatch: pytest.MonkeyPatch) -> ActionPolicy:
    monkeypatch.delenv("ACTION_POLICY_PATH", raising=False)
    return ActionPolicy(engine=engine)


@pytest.mark.spec("ARBORA-IDENTITY-R019.8")
def test_held_action_files_one_control_lease_and_no_graph_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leases = SyncControlLeases()
    engine = _Engine(leases)
    policy = _policy(engine, monkeypatch)

    first = policy.decide(_held())
    again = policy.decide(_held())

    assert first.decision == again.decision == DECISION_QUEUE
    assert first.approval_id and first.approval_id.startswith("action_approval:")
    # The same request reuses its pending approval instead of filing another.
    assert again.approval_id == first.approval_id
    (lease,) = leases.rows.values()
    assert lease["lease_id"] == first.approval_id
    assert (lease["kind"], lease["status"]) == ("action.approval", "active")
    assert lease["grant"]["request_digest"] == _held().digest()
    assert (lease["grant"]["kind"], lease["grant"]["target"]) == (
        "restart_service",
        "svc-a",
    )
    span = lease["hard_expires_at_ms"] - lease["issued_at_ms"]
    assert span == lease["expires_at_ms"] - lease["issued_at_ms"]
    assert span == approval_leases.APPROVAL_LIFETIME_MS
    (issue,) = [call for verb, call in leases.calls if verb == "issue"]
    assert issue["idempotency_key"] == first.approval_id
    # Only audit rows are graph nodes; no approval node is ever written.
    assert {label for label, _ in engine.created.values()} == {"ActionDecision"}


@pytest.mark.spec("ARBORA-IDENTITY-R019.8")
def test_granted_probe_needs_a_consumed_unexpired_lease_of_the_same_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leases = SyncControlLeases()
    engine = _Engine(leases)
    policy = _policy(engine, monkeypatch)
    held = policy.decide(_held())
    approval_id = held.approval_id
    assert approval_id

    # Still pending: nothing is granted.
    assert policy.granted_approval_id(_held()) is None
    approval_leases.decide_approval(engine, approval_id, "approved")
    assert leases.rows[approval_id]["status"] == "consumed"
    assert policy.granted_approval_id(_held()) == approval_id
    granted = policy.decide(_held())
    assert granted.decision == DECISION_ALLOW
    assert granted.approval_id == approval_id

    # Another request (other params, other digest) is not authorized by it.
    other = ActionRequest(
        kind="restart_service", target="svc-a", params={"n": 2}, source="test"
    )
    assert policy.granted_approval_id(other) is None

    digest = _held().digest()
    issued_at = leases.rows[approval_id]["issued_at_ms"]
    assert approval_leases.granted_approval(engine, digest, now_ms=issued_at) == (
        approval_id
    )
    expired_at = leases.rows[approval_id]["expires_at_ms"]
    assert approval_leases.granted_approval(engine, digest, now_ms=expired_at) is None
    leases.rows[approval_id]["status"] = "revoked"
    assert policy.granted_approval_id(_held()) is None


@pytest.mark.spec("ARBORA-IDENTITY-R019.8")
@pytest.mark.parametrize("failure", ["refused", "collision", "no_view"])
def test_issue_refusal_fails_the_hold_closed(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    leases = SyncControlLeases()
    if failure == "refused":

        def refuse(**_request: Any) -> Any:
            raise RuntimeError("ACCESS_DENIED: lease:write")

        leases.issue = refuse  # type: ignore[method-assign]
    elif failure == "collision":
        leases.issue = lambda **_request: {"outcome": "collision", "lease": None}  # type: ignore[method-assign]
    engine = _Engine(None if failure == "no_view" else leases)
    decision = _policy(engine, monkeypatch).decide(_held())
    assert decision.decision == DECISION_UNAVAILABLE
    assert decision.reason == "approval receipt unavailable (fail closed)"
    assert decision.approval_id is None
    assert leases.rows == {}


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


@pytest.mark.spec("ARBORA-IDENTITY-R019.9")
@pytest.mark.asyncio
async def test_fleet_list_and_grant_use_the_approval_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    from arbora.gateway.fleet import fleet_approvals, fleet_grant_approval
    from arbora.mcp_server.composition import install_gateway_application
    from arbora.mcp_server.host_context import compose_host_context
    from arbora.security.session import use_session
    from tests._support import FixedEngineHandles

    leases = SyncControlLeases(tenant="test")
    engine = _Engine(leases)
    pending_id = _policy(engine, monkeypatch).decide(_held()).approval_id
    assert pending_id
    leases.rows["action_approval:foreign"] = {
        **leases.rows[pending_id],
        "lease_id": "action_approval:foreign",
        "kind": "browser.control",
    }
    host = compose_host_context(FixedEngineHandles(engine))
    install_gateway_application(
        fleet=host.fleet,
        approvals=host.policy.approvals,
        engine_provider=host.engine.served,
        caches=host.caches,
    )
    app = Starlette(
        routes=[
            Route("/fleet/approvals", fleet_approvals, methods=["GET"]),
            Route("/fleet/approvals/grant", fleet_grant_approval, methods=["POST"]),
        ]
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://host"
    ) as client:
        with use_session(_verified_session()):
            listed = (await client.get("/fleet/approvals")).json()
            granted = await client.post(
                "/fleet/approvals/grant",
                json={"job_id": pending_id, "decision": "approve"},
            )
            again = await client.post(
                "/fleet/approvals/grant",
                json={"job_id": pending_id, "decision": "deny"},
            )
            foreign = await client.post(
                "/fleet/approvals/grant",
                json={"job_id": "action_approval:foreign", "decision": "approve"},
            )
            after = (await client.get("/fleet/approvals")).json()

    assert listed["status"] == "success"
    (entry,) = listed["pending"]
    assert (entry["id"], entry["kind"], entry["target"]) == (
        pending_id,
        "restart_service",
        "svc-a",
    )
    assert entry["status"] == "active"
    assert granted.json() == {
        "status": "success",
        "result": {"approval_id": pending_id, "decision": "approved"},
    }
    assert leases.rows[pending_id]["status"] == "consumed"
    # A decided approval and a lease of another kind are not pending: 409.
    assert again.status_code == foreign.status_code == 409
    assert after == {"status": "success", "pending": []}
    assert {label for label, _ in engine.created.values()} == {"ActionDecision"}
