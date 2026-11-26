"""The engine answer sets a plan's gate; no answer is the strictest gate (ARBORA-OPS-R918.2)."""

from __future__ import annotations

import asyncio
from typing import Any

from arbora.access.approval_leases import ACTION_APPROVAL_KIND
from arbora.api.invoke.gate_class import (
    DECIDE_SCOPE,
    DEFAULT_APPROVER_ROLES,
    GATE_CLASSES,
    NO_APPROVAL,
    STRICT_DEFAULT,
    STRICTEST_CLASS,
    no_engine_answer,
    resolve_gate,
)
from arbora.api.invoke.plan import EgPlanStore, PlanBinding, approval_id_for


class _Leases:
    def __init__(self) -> None:
        self.issued: list[dict[str, Any]] = []

    async def issue(self, **request: Any) -> dict[str, Any]:
        self.issued.append(request)
        return {"outcome": "issued"}

    async def get(self, *, tenant: str, lease_id: str) -> dict[str, Any] | None:
        return next((r for r in self.issued if r["lease_id"] == lease_id), None)


class _Client:
    def __init__(self) -> None:
        self.control_leases = _Leases()


def _binding() -> PlanBinding:
    return PlanBinding(
        "publish.post", "d" * 64, "agent-1", "tenant-a", "r1", "reg", "write", "plan"
    )


def _reader(answer: Any):
    async def read(_op: str, _tenant: str) -> Any:
        if isinstance(answer, Exception):
            raise answer
        return answer

    return read


# spec: ARBORA-OPS-R918.2
def test_engine_answer_sets_class_roles_expiry_and_default_is_strict() -> None:
    answer = {
        "gate_class": "publishing",
        "approver_roles": ["editors"],
        "expires_in_ms": 3_600_000,
    }
    gate = asyncio.run(resolve_gate(_reader(answer), "publish.post", "tenant-a"))
    assert (gate.gate_class, gate.approver_roles, gate.expires_in_ms) == (
        "publishing",
        ("editors",),
        3_600_000,
    )
    assert gate.source == "engine"

    # No answer, an unreadable answer and a failed read are all the strict default.
    for unanswered in (
        None,
        {"gate_class": "free_for_all", "approver_roles": ["a"], "expires_in_ms": 5},
        {"gate_class": "publishing", "approver_roles": [], "expires_in_ms": 5},
        {"gate_class": "publishing", "approver_roles": ["a"], "expires_in_ms": 0},
        {"gate_class": "publishing", "approver_roles": ["a"], "expires_in_ms": True},
        "text",
        RuntimeError("engine down"),
    ):
        resolved = asyncio.run(resolve_gate(_reader(unanswered), "op", "tenant-a"))
        assert resolved == STRICT_DEFAULT
    assert STRICT_DEFAULT.gate_class == STRICTEST_CLASS == GATE_CLASSES[0]
    assert STRICT_DEFAULT.approver_roles == DEFAULT_APPROVER_ROLES
    assert STRICT_DEFAULT.as_grant()["decide_scope"] == DECIDE_SCOPE
    assert asyncio.run(no_engine_answer("op", "tenant-a")) is None


# spec: ARBORA-OPS-R918.2
def test_mint_stores_the_gate_and_files_one_approval_item_per_engine_gated_plan() -> (
    None
):
    client = _Client()
    store = EgPlanStore(client, lease_kind="arbora.plan")
    gate = asyncio.run(
        resolve_gate(
            _reader(
                {
                    "gate_class": "money_movement",
                    "approver_roles": ["finance"],
                    "expires_in_ms": 120_000,
                }
            ),
            "pay.invoice",
            "tenant-a",
        )
    )

    plan_ref = asyncio.run(store.issue(_binding(), {"x": 1}, gate))

    plan, approval = client.control_leases.issued
    assert plan["grant"]["gate_class"] == "money_movement"
    assert plan["grant"]["approver_roles"] == ["finance"]
    assert approval["kind"] == ACTION_APPROVAL_KIND
    assert approval["lease_id"] == approval_id_for(plan_ref)
    assert approval["grant"]["proposer"] == "agent-1"
    assert approval["grant"]["request_digest"] == "d" * 64
    assert approval["expires_at_ms"] - approval["issued_at_ms"] == 120_000

    # A plan with no engine answer carries the strict default but files no item.
    other = _Client()
    asyncio.run(
        EgPlanStore(other, lease_kind="arbora.plan").issue(
            _binding(), {"x": 1}, STRICT_DEFAULT
        )
    )
    assert len(other.control_leases.issued) == 1
    assert other.control_leases.issued[0]["grant"]["gate_class"] == STRICTEST_CLASS


# spec: ARBORA-OPS-R918.2
def test_engine_gate_reader_maps_the_decision_answer_and_unresolved_is_strict() -> None:
    from arbora.api.invoke.gate_class import engine_gate_reader

    answers = {
        "deploy.promotions.apply": {
            "gate": "single",
            "gate_class": "publishing",
            "approver_roles": ["release-managers"],
            "expires_after_ms": 90_000,
        },
        "open.op": {"gate": "unresolved", "gate_class": None, "approver_roles": []},
        "free.op": {"gate": "none", "gate_class": None, "approver_roles": []},
    }
    sent: list[tuple[str, dict[str, Any], str]] = []

    async def send(method: str, params: dict[str, Any], tenant: str) -> Any:
        sent.append((method, params, tenant))
        return answers[params["request"]["operation"]]

    reader = engine_gate_reader(send)
    gate = asyncio.run(resolve_gate(reader, "deploy.promotions.apply", "tenant-a"))
    assert (gate.gate_class, gate.approver_roles, gate.expires_in_ms, gate.source) == (
        "publishing",
        ("release-managers",),
        90_000,
        "engine",
    )
    assert sent[0] == (
        "ApprovalGateResolve",
        {"request": {"operation": "deploy.promotions.apply"}},
        "tenant-a",
    )
    # Unresolved is no answer, so the strict default applies.
    assert asyncio.run(resolve_gate(reader, "open.op", "tenant-a")) == STRICT_DEFAULT
    # An explicit engine ``none`` is the graph's answer: no approval, never strict.
    free = asyncio.run(resolve_gate(reader, "free.op", "tenant-a"))
    assert free == NO_APPROVAL and free != STRICT_DEFAULT

    # An explicit-none plan files no approval item and never waits for one.
    client = _Client()
    store = EgPlanStore(client, lease_kind="arbora.plan")
    plan_ref = asyncio.run(store.issue(_binding(), {"x": 1}, free))
    assert [row["kind"] for row in client.control_leases.issued] == ["arbora.plan"]
    assert client.control_leases.issued[0]["grant"]["gate_source"] == "none"
    assert asyncio.run(store.approval_pending(plan_ref, "tenant-a")) is False
