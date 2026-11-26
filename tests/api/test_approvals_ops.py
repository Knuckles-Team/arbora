"""The approval inbox reads and the decision write (ARBORA-OPS-R918.1, R918.3)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import approvals
from arbora.api.registry import AuditClass

NOW = 1_000_000


class _Leases:
    """An in-memory engine lease view for ``action.approval`` rows."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.transitions: list[dict[str, Any]] = []

    def add(
        self,
        suffix: str,
        *,
        proposer: str = "agent-1",
        gate_class: str = "publishing",
        roles: tuple[str, ...] = ("editors",),
        status: str = "active",
        expires: int = NOW + 60_000,
    ) -> str:
        lease_id = f"action_approval:{suffix}"
        self.rows[lease_id] = {
            "lease_id": lease_id,
            "kind": "action.approval",
            "status": status,
            "revision": 1,
            "issued_at_ms": NOW - 1,
            "expires_at_ms": expires,
            "hard_expires_at_ms": expires,
            "grant": {
                "request_digest": f"digest-{suffix}",
                "op_id": "publish.post",
                "proposer": proposer,
                "gate_class": gate_class,
                "approver_roles": list(roles),
            },
        }
        return lease_id

    async def get(self, *, tenant: str, lease_id: str) -> dict[str, Any] | None:
        return self.rows.get(lease_id)

    async def list(self, *, status: str, kind: str, **_kw: Any) -> dict[str, Any]:
        return {
            "leases": [
                r
                for r in self.rows.values()
                if r["status"] == status and r["kind"] == kind
            ]
        }

    async def transition(self, **request: Any) -> dict[str, Any]:
        self.transitions.append(request)
        row = self.rows[request["lease_id"]]
        row["status"] = request["to"]
        row["revision"] += 1
        return {"outcome": "applied"}


def _context(leases: _Leases, principal: str, *roles: str) -> Any:
    caller = SimpleNamespace(principal=principal, tenant="tenant-a", roles=roles)
    return SimpleNamespace(
        caller=caller,
        client=SimpleNamespace(control_leases=leases),
        services={"now_ms": lambda: NOW},
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _list(context: Any, **params: Any) -> list[dict[str, Any]]:
    result = _run(
        approvals.handle_approvals_list(context, {"limit": 50, **params}, None)  # type: ignore[arg-type]
    )
    return result["items"]


# spec: ARBORA-OPS-R918.1
def test_list_filters_by_gate_class_and_approver_role() -> None:
    leases = _Leases()
    pub = leases.add("pub")
    money = leases.add("money", gate_class="money_movement", roles=("finance",))
    leases.add("decided", status="consumed")
    leases.add("late", expires=NOW - 1)
    leases.add("theirs", proposer="someone", roles=("legal",))
    editor = _context(leases, "ed-1", "editors", "finance")

    by_class = _list(editor, gate_class="money_movement")
    assert [i["approval_id"] for i in by_class] == [money]
    by_role = _list(editor, approver_role="editors", state="pending")
    assert [i["approval_id"] for i in by_role] == [pub]
    states = {i["approval_id"]: i["state"] for i in _list(editor)}
    assert states[pub] == "pending"
    assert states["action_approval:decided"] == "approved"
    assert states["action_approval:late"] == "expired"
    # An item whose role the caller lacks, from another proposer, is not listed.
    assert "action_approval:theirs" not in states
    # A proposer sees its own proposals without the approver role.
    own = _list(_context(leases, "agent-1"))
    assert {i["approval_id"] for i in own} >= {pub, money}
    got = _run(approvals.handle_approvals_get(editor, {"approval_id": pub}, None))  # type: ignore[arg-type]
    assert got["item"]["gate_class"] == "publishing"
    with pytest.raises(OperationRefused) as hidden:
        _run(
            approvals.handle_approvals_get(
                editor,
                {"approval_id": "action_approval:theirs"},
                None,  # type: ignore[arg-type]
            )
        )
    assert hidden.value.code == "NOT_FOUND"


def _decide(
    context: Any, approval_id: str, decision: str = "approved", **over: Any
) -> Any:
    params = {
        "approval_id": approval_id,
        "decision": decision,
        "plan_digest": over.pop("plan_digest", "digest-" + approval_id.split(":")[1]),
        "reason": "reviewed",
        **over,
    }
    return _run(approvals.handle_approvals_decide(context, params, None))  # type: ignore[arg-type]


# spec: ARBORA-OPS-R918.3
def test_decide_refuses_self_approval_and_records_provenance() -> None:
    leases = _Leases()
    item = leases.add("one", roles=("editors", "agents"))

    with pytest.raises(OperationRefused) as own:
        _decide(_context(leases, "agent-1", "agents"), item)
    assert own.value.code == "SELF_APPROVAL"
    with pytest.raises(OperationRefused) as digest:
        _decide(_context(leases, "ed-1", "editors"), item, plan_digest="other")
    assert digest.value.code == "PLAN_MISMATCH"
    with pytest.raises(OperationRefused) as role:
        _decide(_context(leases, "x-1", "intern"), item)
    assert role.value.code == "NOT_FOUND"  # no existence oracle
    assert leases.transitions == []

    approver = _context(leases, "ed-1", "editors")
    first = _decide(approver, item)
    assert first["changed"] is True
    assert first["item"]["state"] == "approved"
    assert leases.transitions[0]["to"] == "consumed"
    assert leases.transitions[0]["expected_revision"] == 1

    # A repeated decision changes nothing; the opposite decision is refused.
    again = _decide(approver, item)
    assert again["changed"] is False
    assert len(leases.transitions) == 1
    with pytest.raises(OperationRefused) as flipped:
        _decide(approver, item, decision="denied")
    assert flipped.value.code == "ALREADY_DECIDED"

    # The pipeline audits the write: its params digest binds item, digest,
    # decision and reason; the engine adds the approver and the time.
    decide = next(op for op in approvals.operations() if op.id == "approvals.decide")
    assert decide.audit is AuditClass.EVENT
    assert set(decide.params.model_fields) >= {
        "approval_id",
        "decision",
        "plan_digest",
        "reason",
    }


# spec: ARBORA-OPS-R918.3
def test_expired_item_refuses_decision_and_denial_revokes() -> None:
    leases = _Leases()
    late = leases.add("late", expires=NOW - 1)
    with pytest.raises(OperationRefused) as expired:
        _decide(_context(leases, "ed-1", "editors"), late)
    assert expired.value.code == "PLAN_EXPIRED"
    fresh = leases.add("fresh")
    result = _decide(_context(leases, "ed-1", "editors"), fresh, decision="denied")
    assert result["item"]["state"] == "denied"
    assert leases.transitions[-1]["to"] == "revoked"


# spec: ARBORA-OPS-R918.4
def test_expired_item_refuses_consume_and_lists_expired() -> None:
    from arbora.api.invoke.plan import EgPlanStore, PlanBinding, approval_id_for
    from tests.api.test_invoke_contract import FixtureLeases

    class Clock:
        now = NOW
        reads = 0

        def __call__(self) -> int:
            self.reads += 1
            return self.now

    class Rows(FixtureLeases):
        async def list(self, *, status: str, kind: str, **_kw: Any) -> dict[str, Any]:
            return {
                "leases": [
                    r
                    for r in self.rows.values()
                    if r["status"] == status and r["kind"] == kind
                ]
            }

    leases, clock = Rows(), Clock()
    store = EgPlanStore(
        SimpleNamespace(control_leases=leases), lease_kind="plan", clock_ms=clock
    )
    from arbora.api.invoke.gate_class import GateResolution

    gate = GateResolution("publishing", ("editors",), 30_000, "engine")
    binding = PlanBinding(
        "op", "d" * 64, "agent-1", "tenant-a", "r", "g", "write", "plan"
    )
    ref = _run(store.issue(binding, {"x": 1}, gate))
    item_id = approval_id_for(ref)

    clock.now = NOW + 30_001
    clock.reads = 0
    refusal = _run(store.consume(ref, binding))
    assert refusal is not None and refusal.code == "PLAN_EXPIRED"
    assert clock.reads == 1  # the clock is read once per consume
    assert leases.rows[ref]["status"] == "active" and leases.transitions == 0

    editor = SimpleNamespace(
        caller=SimpleNamespace(principal="ed", tenant="tenant-a", roles=("editors",)),
        client=SimpleNamespace(control_leases=leases),
        services={"now_ms": lambda: clock.now},
    )
    listed = _run(approvals.handle_approvals_list(editor, {"limit": 50}, None))  # type: ignore[arg-type]
    assert {i["approval_id"]: i["state"] for i in listed["items"]}[item_id] == "expired"
