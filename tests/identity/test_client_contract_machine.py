"""The OAuth client contract reconciler is one explicit state machine (ARBORA-IDENTITY-R030.4.2)."""

from __future__ import annotations

import itertools
import random
import threading
from collections.abc import Callable

import pytest

from arbora.identity import client_contract as cc
from arbora.identity.client_contract import (
    CONTRACT_EDGES,
    ContractEdge,
    ContractEvent,
    ContractState,
    IllegalContractTransition,
    ReconcileResult,
)
from tests.identity.test_client_contract import CONFIG, FakeRealm, factory

OK = ReconcileResult(True)
ERR = ReconcileResult(False, "client_missing", ("client:arbora-native-mcp",))

_PATH: dict[ContractState, tuple[tuple[ContractEvent, ReconcileResult | None], ...]] = {
    ContractState.UNCHECKED: (),
    ContractState.RECONCILING: ((ContractEvent.RECONCILE_START, None),),
    ContractState.CONVERGED: (
        (ContractEvent.RECONCILE_START, None),
        (ContractEvent.RECONCILE_OK, OK),
    ),
    ContractState.DEGRADED: (
        (ContractEvent.RECONCILE_START, None),
        (ContractEvent.RECONCILE_ERR, ERR),
    ),
    ContractState.STOPPED: ((ContractEvent.STOP, None),),
}


def _in(state: ContractState) -> cc.ClientContractReconciler:
    machine = cc.ClientContractReconciler()
    for event, result in _PATH[state]:
        machine.apply(event, result=result)
    assert machine.state is state
    return machine


def _result_for(event: ContractEvent) -> ReconcileResult | None:
    return {ContractEvent.RECONCILE_OK: OK, ContractEvent.RECONCILE_ERR: ERR}.get(event)


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.2")
@pytest.mark.parametrize("edge", CONTRACT_EDGES, ids=lambda e: e.edge_id.lower())
def test_legal_edge(edge: ContractEdge) -> None:
    machine = _in(edge.source)
    assert machine.apply(edge.event, result=_result_for(edge.event)) is edge.target
    assert machine.state is edge.target
    if edge.target is ContractState.DEGRADED:
        assert (machine.failure_code, machine.attempts, machine.items) == (
            "client_missing",
            1,
            ERR.items,
        )
    if edge.target is ContractState.CONVERGED:
        assert (machine.failure_code, machine.attempts) == (None, 0)


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.2")
def test_every_illegal_pair_is_refused() -> None:
    legal = {(e.source, e.event) for e in CONTRACT_EDGES}
    pairs = [
        p for p in itertools.product(ContractState, ContractEvent) if p not in legal
    ]
    assert len(CONTRACT_EDGES) == 9 and len(pairs) == 21
    cases = [(s, e, _result_for(e)) for s, e in pairs]
    # The failed guard: reconcile_ok whose read-back does not match.
    cases.append((ContractState.RECONCILING, ContractEvent.RECONCILE_OK, ERR))
    for state, event, result in cases:
        machine = _in(state)
        before = (machine.state, machine.attempts, machine.failure_code, machine.items)
        with pytest.raises(IllegalContractTransition) as refused:
            machine.apply(event, result=result)
        error = refused.value
        assert (error.code, error.machine, error.from_state, error.event) == (
            "CLIENT_CONTRACT_TRANSITION_ILLEGAL",
            "client_contract",
            state,
            event,
        )
        assert (
            machine.state,
            machine.attempts,
            machine.failure_code,
            machine.items,
        ) == before


class SteppedStop(threading.Event):
    def __init__(self, until: Callable[[], bool]) -> None:
        super().__init__()
        self.until = until
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(float(timeout or 0.0))
        if self.until():
            self.set()
        return self.is_set()


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.2")
def test_backoff_from_degraded_and_interval_from_converged() -> None:
    seeded = random.Random(3)
    expected = [cc.next_retry_delay(n, seeded) for n in range(1, 11)]
    for delay, n in zip(expected, range(1, 11), strict=True):
        base = min(300.0, 2.0 ** (n - 1))
        assert 0.5 * base <= delay <= base

    realm = FakeRealm()
    realm.down = True
    supervisor = cc.ClientContractSupervisor(
        CONFIG, admin_factory=factory(realm), rng=random.Random(3)
    )
    stop = SteppedStop(lambda: False)
    stop.until = lambda: len(stop.waits) >= 10
    supervisor.stop_event = stop
    supervisor.loop()
    assert supervisor.reconciler.state is ContractState.DEGRADED
    assert stop.waits == pytest.approx(expected)

    # Once the provider answers, the next retry converges, then waits the interval.
    realm.down = False
    stop.clear()
    stop.waits.clear()
    machine = supervisor.reconciler
    stop.until = lambda: machine.state is ContractState.CONVERGED
    machine.apply(ContractEvent.RETRY_ELAPSED)
    cc.run_pass(machine, CONFIG, factory(realm))
    assert machine.state is ContractState.CONVERGED
    fresh = cc.ClientContractSupervisor(
        CONFIG, admin_factory=factory(realm), rng=random.Random(3)
    )
    fresh.stop_event = stop
    stop.until = lambda: fresh.reconciler.state is ContractState.CONVERGED
    fresh.loop()
    assert fresh.reconciler.state is ContractState.CONVERGED
    assert 150.0 <= stop.waits[-1] <= 300.0
