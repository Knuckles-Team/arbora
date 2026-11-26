"""An agent proposal reaches a human decision through the invoke pipeline (ARBORA-OPS-R918.7)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from test_invoke_contract import (
    FixtureLeases,
    operation,
    run,
    setup_services,
)

from arbora.api.invoke.plan import EgPlanStore, approval_id_for
from arbora.api.ops import approvals
from arbora.api.registry import Effect, Registry
from tests.api._support import make_verified_caller

NOW = 5_000_000
PUBLISHING = {
    "gate_class": "publishing",
    "approver_roles": ["editors"],
    "expires_in_ms": 60_000,
}


class Leases(FixtureLeases):
    """The fixture lease view plus the listing the inbox reads."""

    async def list(self, *, status: str, kind: str, **_kw: Any) -> dict[str, Any]:
        return {
            "leases": [
                r
                for r in self.rows.values()
                if r["status"] == status and r["kind"] == kind
            ]
        }


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> int:
        return self.now


def _stack() -> tuple[Any, Any, Any, Leases, Clock]:
    leases, clock = Leases(), Clock()

    async def reader(_op: str, _tenant: str) -> dict[str, Any]:
        return PUBLISHING

    services, runtime, _, journal, _ = setup_services(
        registry=Registry([operation(effect=Effect.DESTRUCTIVE)]),
        gate_reader=reader,
        external_effect_operations=frozenset(),
    )
    services = type(services)(
        **{
            **{f: getattr(services, f) for f in services.__dataclass_fields__},
            "plans": EgPlanStore(
                SimpleNamespace(control_leases=leases),
                lease_kind="fixture.plan",
                seal_key=b"x" * 32,
                clock_ms=clock,
            ),
        }
    )
    return services, runtime, journal, leases, clock


def _inbox(leases: Leases, principal: str, *roles: str) -> Any:
    caller = SimpleNamespace(principal=principal, tenant="tenant:one", roles=roles)
    return SimpleNamespace(
        caller=caller,
        client=SimpleNamespace(control_leases=leases),
        services={"now_ms": lambda: NOW},
    )


def _agent() -> Any:
    return make_verified_caller("example:write")(principal="agent:one")


def _propose(services: Any) -> str:
    preview = run(services, supplied=_agent())
    assert preview.code == "CONFIRMATION_REQUIRED"
    return preview.details["plan_ref"]


def _decide(leases: Leases, who: Any, ref: str, decision: str = "approved") -> Any:
    item = leases.rows[approval_id_for(ref)]
    return asyncio.run(
        approvals.handle_approvals_decide(
            who,
            {
                "approval_id": item["lease_id"],
                "decision": decision,
                "plan_digest": item["grant"]["request_digest"],
                "reason": "reviewed",
            },
            None,  # type: ignore[arg-type]
        )
    )


# spec: ARBORA-OPS-R918.7
def test_proposal_to_decision_runs_once_and_refusals_run_nothing() -> None:
    services, runtime, journal, leases, clock = _stack()
    editor = _inbox(leases, "person:ed", "editors")

    # An unapproved plan runs no effect, whatever the proposer does.
    ref = _propose(services)
    pending = asyncio.run(
        approvals.handle_approvals_list(editor, {"limit": 50, "state": "pending"}, None)  # type: ignore[arg-type]
    )
    assert [i["gate_class"] for i in pending["items"]] == ["publishing"]
    unapproved = run(services, supplied=_agent(), plan_ref=ref)
    assert unapproved.code == "APPROVAL_REQUIRED" and not runtime.calls

    # The proposer cannot approve its own item.
    own = _inbox(leases, "agent:one", "editors")
    with pytest.raises(Exception) as self_approval:
        _decide(leases, own, ref)
    assert getattr(self_approval.value, "code", "") == "SELF_APPROVAL"
    assert not runtime.calls

    # A denial leaves no effect.
    denied_ref = _propose(services)
    _decide(leases, editor, denied_ref, "denied")
    denied = run(services, supplied=_agent(), plan_ref=denied_ref)
    assert denied.code == "APPROVAL_DENIED" and not runtime.calls

    # An expiry leaves no effect.
    late_ref = _propose(services)
    clock.now += 60_001
    expired = run(services, supplied=_agent(), plan_ref=late_ref)
    assert expired.code == "PLAN_EXPIRED" and not runtime.calls
    clock.now = NOW

    # An approved plan runs once; a replay settles through the journal.
    _decide(leases, editor, ref)
    first = run(services, supplied=_agent(), plan_ref=ref)
    assert first.code == "OK"
    assert run(services, supplied=_agent(), plan_ref=ref) == first
    assert len(runtime.calls) == 1
    done = [r for _, r in journal.rows.values() if r.outcome.code == "OK"]
    assert len(done) == 1
