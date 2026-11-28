"""The child runtime and circuit breaker machines (ARBORA-FLEET-R091)."""

from __future__ import annotations

from itertools import product

import pytest

from arbora.api.domain.fleet.plan_confirmation import PlanConfirmations
from arbora.core.settings import RuntimeConfig
from arbora.fleet import child_machines as m
from arbora.fleet.child_resilience import ChildCircuitBreaker, ChildRuntime
from tests.architecture.single_writer import field_writers

CS, CE = m.ChildState, m.ChildEvent
BS, BE = m.BreakerState, m.BreakerEvent
RESILIENCE = "arbora/fleet/child_resilience.py"


def _runtime(state: m.ChildState) -> ChildRuntime:
    runtime = ChildRuntime(
        "probe", confirmations=PlanConfirmations(), config=RuntimeConfig()
    )
    runtime.state = state
    return runtime


@pytest.mark.spec("ARBORA-FLEET-R091")
@pytest.mark.parametrize(
    ("source", "event", "target"),
    [
        pytest.param(CS.STARTING, CE.CONNECT, CS.UP, id="starting-connect"),
        pytest.param(CS.UP, CE.CONNECT, CS.UP, id="up-connect"),
        pytest.param(CS.RESTARTING, CE.CONNECT, CS.UP, id="restarting-connect"),
        pytest.param(CS.UP, CE.DISCONNECT, CS.RESTARTING, id="up-disconnect"),
        pytest.param(
            CS.RESTARTING, CE.DISCONNECT, CS.RESTARTING, id="restarting-disconnect"
        ),
        pytest.param(CS.STARTING, CE.FAIL, CS.FAILED, id="starting-fail"),
        pytest.param(CS.UP, CE.FAIL, CS.FAILED, id="up-fail"),
        pytest.param(CS.RESTARTING, CE.FAIL, CS.FAILED, id="restarting-fail"),
        pytest.param(CS.STARTING, CE.CLOSE, CS.CLOSED, id="starting-close"),
        pytest.param(CS.UP, CE.CLOSE, CS.CLOSED, id="up-close"),
        pytest.param(CS.RESTARTING, CE.CLOSE, CS.CLOSED, id="restarting-close"),
        pytest.param(CS.FAILED, CE.CLOSE, CS.CLOSED, id="failed-close"),
    ],
)
def test_child_legal_edge_succeeds(
    source: m.ChildState, event: m.ChildEvent, target: m.ChildState
) -> None:
    assert m.child_transition(source, event) is target
    runtime = _runtime(source)
    runtime._fire(event)
    assert runtime.state is target


@pytest.mark.spec("ARBORA-FLEET-R091")
def test_child_every_illegal_pair_is_refused_and_the_state_is_unchanged() -> None:
    legal = set(m.CHILD_EDGES)
    refused = 0
    for state, event in product(CS, CE):
        if (state, event) in legal or state is CS.CLOSED:
            continue
        runtime = _runtime(state)
        with pytest.raises(m.ChildTransitionRefused) as raised:
            runtime._fire(event)
        assert raised.value.code == "CHILD_TRANSITION_ILLEGAL"
        assert raised.value.machine == "child_runtime"
        assert (raised.value.from_state, raised.value.event) == (state, event)
        assert runtime.state is state
        refused += 1
    assert len(CS) * len(CE) - len(legal) == 8
    assert refused == 8 - len(CE)  # the four closed pairs are ignored, not refused


@pytest.mark.spec("ARBORA-FLEET-R091")
def test_a_closed_runtime_ignores_every_event() -> None:
    for event in CE:
        runtime = _runtime(CS.CLOSED)
        runtime._fire(event)
        assert runtime.state is CS.CLOSED


@pytest.mark.spec("ARBORA-FLEET-R091")
def test_child_initial_terminals_and_stored_values() -> None:
    assert m.CHILD_INITIAL is CS.STARTING
    exits = {source for source, _ in m.CHILD_EDGES}
    assert set(m.CHILD_TERMINALS) == set(CS) - exits
    assert [s.value for s in CS] == ["starting", "up", "restarting", "failed", "closed"]
    assert _runtime(CS.UP).status()["state"] == "up"
    assert CS.UP == "up"


@pytest.mark.spec("ARBORA-FLEET-R091.1")
@pytest.mark.parametrize(
    ("source", "event", "target"),
    [
        pytest.param(BS.CLOSED, BE.TRIP, BS.OPEN, id="closed-trip"),
        pytest.param(BS.OPEN, BE.TRIP, BS.OPEN, id="open-trip"),
        pytest.param(BS.HALF_OPEN, BE.TRIP, BS.OPEN, id="half_open-trip"),
        pytest.param(BS.OPEN, BE.PROBE, BS.HALF_OPEN, id="open-probe"),
        pytest.param(BS.CLOSED, BE.RECOVER, BS.CLOSED, id="closed-recover"),
        pytest.param(BS.OPEN, BE.RECOVER, BS.CLOSED, id="open-recover"),
        pytest.param(BS.HALF_OPEN, BE.RECOVER, BS.CLOSED, id="half_open-recover"),
    ],
)
def test_breaker_legal_edge_succeeds(
    source: m.BreakerState, event: m.BreakerEvent, target: m.BreakerState
) -> None:
    assert m.breaker_transition(source, event) is target
    breaker = ChildCircuitBreaker("probe", 3, 30.0)
    breaker._state = source
    breaker._fire(event)
    assert breaker.state is target


@pytest.mark.spec("ARBORA-FLEET-R091.1")
def test_breaker_every_illegal_pair_is_refused_and_the_state_is_unchanged() -> None:
    legal = set(m.BREAKER_EDGES)
    refused = 0
    for state, event in product(BS, BE):
        if (state, event) in legal:
            continue
        breaker = ChildCircuitBreaker("probe", 3, 30.0)
        breaker._state = state
        with pytest.raises(m.BreakerTransitionRefused) as raised:
            breaker._fire(event)
        assert raised.value.code == "BREAKER_TRANSITION_ILLEGAL"
        assert raised.value.machine == "child_breaker"
        assert (raised.value.from_state, raised.value.event) == (state, event)
        assert breaker.state is state
        refused += 1
    assert refused == len(BS) * len(BE) - len(legal) == 2


@pytest.mark.spec("ARBORA-FLEET-R091.1")
def test_breaker_initial_terminals_and_stored_values() -> None:
    assert m.BREAKER_INITIAL is BS.CLOSED
    assert m.BREAKER_TERMINALS == ()
    assert [s.value for s in BS] == ["closed", "open", "half_open"]
    assert ChildCircuitBreaker("probe", 3, 30.0).state == "closed"


@pytest.mark.spec("ARBORA-FLEET-R091")
@pytest.mark.spec("ARBORA-FLEET-R091.1")
def test_only_fire_and_init_write_the_state_fields() -> None:
    writers = {
        pair
        for pair in field_writers("arbora/fleet", {"state", "_state"})
        if pair[0] == RESILIENCE
    }
    assert writers == {(RESILIENCE, "__init__"), (RESILIENCE, "_fire")}
