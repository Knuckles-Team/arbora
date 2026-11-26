"""The plan lease is one machine with distinct consumed and expired refusals (ARBORA-OPS-R901)."""

from __future__ import annotations

import itertools

import pytest
from test_invoke_contract import operation, run, setup_services

from arbora.api.invoke.plan import (
    PLAN_EDGES,
    TERMINAL_PLAN_STATES,
    IllegalPlanTransition,
    PlanEvent,
    PlanState,
    _live_lease_refusal,
    plan_state,
    transition,
)
from arbora.api.registry import Effect

pytestmark = pytest.mark.spec("ARBORA-OPS-R901")


@pytest.mark.parametrize(
    ("state", "event", "target"),
    [(s, e, t) for (s, e), t in PLAN_EDGES.items()],
    ids=[f"{s.value}-{e.value}-{t.value}" for (s, e), t in PLAN_EDGES.items()],
)
def test_legal_edge(state: PlanState, event: PlanEvent, target: PlanState) -> None:
    assert transition(state, event) is target


def test_every_illegal_pair_is_refused() -> None:
    illegal = [
        pair
        for pair in itertools.product(PlanState, PlanEvent)
        if pair not in PLAN_EDGES
    ]
    assert len(PLAN_EDGES) == 6 and len(illegal) == 24
    for state, event in illegal:
        with pytest.raises(IllegalPlanTransition) as refused:
            transition(state, event)
        assert refused.value.code == "PLAN_TRANSITION_ILLEGAL"
        assert (
            refused.value.machine,
            refused.value.from_state,
            refused.value.event,
        ) == (
            "plan_lease",
            state,
            event,
        )
    assert TERMINAL_PLAN_STATES == {PlanState.DONE, PlanState.FAILED, PlanState.EXPIRED}
    assert all(
        state not in {s for s, _ in PLAN_EDGES}
        for state in TERMINAL_PLAN_STATES - {PlanState.EXPIRED}
    )


def test_engine_lease_maps_to_a_machine_state() -> None:
    live = {"status": "active", "hard_expires_at_ms": 100}
    assert plan_state(live, 99) is PlanState.ISSUED
    assert plan_state(live, 100) is PlanState.EXPIRED
    assert plan_state({**live, "status": "consumed"}, 5) is PlanState.CONSUMED
    assert plan_state({**live, "status": "revoked"}, 5) is PlanState.EXPIRED


def test_second_confirm_is_plan_consumed_and_late_confirm_is_plan_expired() -> None:
    services, _runtime, _audit, _, leases = setup_services(
        operation(effect=Effect.DESTRUCTIVE)
    )
    reference = run(services).details["plan_ref"]
    assert run(services, plan_ref=reference).code == "OK"
    lease = dict(leases.rows[reference])
    assert lease["status"] == "consumed"
    # A second consume reads a consumed lease; an expired one reads as expired.
    assert _live_lease_refusal(lease, "fixture.plan", 1).code == "PLAN_CONSUMED"
    late = {**lease, "status": "active", "hard_expires_at_ms": 0}
    assert _live_lease_refusal(late, "fixture.plan", 1).code == "PLAN_EXPIRED"
    assert (
        _live_lease_refusal(
            {**lease, "status": "active", "hard_expires_at_ms": 9}, "fixture.plan", 1
        )
        is None
    )


def test_served_preview_confirm_runs_through_the_machine() -> None:
    services, runtime, _audit, _, leases = setup_services(
        operation(effect=Effect.DESTRUCTIVE)
    )
    preview = run(services)
    assert preview.code == "CONFIRMATION_REQUIRED" and not runtime.calls
    reference = preview.details["plan_ref"]
    assert plan_state(leases.rows[reference], 0) is PlanState.ISSUED
    assert run(services, plan_ref=reference).code == "OK" and len(runtime.calls) == 1
    assert plan_state(leases.rows[reference], 0) is PlanState.CONSUMED
