"""Provisioning at scale through the served path (ARBORA-FLEET-R024.5 to R024.11)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.api.contracts.provisioning_capacity import AggregationRef
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.agents import handle_agent
from arbora.api.registry import OpSpec
from arbora.core.provision_errors import REFUSALS, ProvisionRefused
from arbora.core.settings import RuntimeConfig
from arbora.fleet.provision_service import ProvisioningService
from arbora.fleet.provisioning import (
    PROVISION_PROOF_KIND,
    max_concurrent_instances,
    provision_instances,
)
from arbora.run_origin import RunOriginRegistry
from tests._eg_fakes import service_session
from tests.fleet._provisioning_support import (
    MATRIX,
    DisposableAdapter,
    guards,
    make_provisioner,
    new_run,
    request,
)


def _run(sdk: Any, req: Any, *, mode: str = "ok", total: int = 200, **kw: Any) -> Any:
    adapter = kw.pop("adapter", None) or DisposableAdapter(mode)
    ledger, journal = kw.pop("run", None) or new_run(total)
    provisioner = kw.get("provisioner") or make_provisioner(sdk, adapter)
    return (
        adapter,
        ledger,
        journal,
        lambda r=req, **o: provision_instances(
            r,
            provisioner=provisioner,
            ledger=ledger,
            guards=o.pop("guards", guards()),
            journal=journal,
            clock=lambda: 7,
            **o,
        ),
    )


@pytest.mark.spec("ARBORA-FLEET-R024.5")
def test_two_hundred_concurrent_instances(sdk: Any) -> None:
    adapter = DisposableAdapter()
    provisioner = make_provisioner(sdk, adapter)
    ledger, journal = new_run(200)

    def one(n: int) -> Any:
        return provision_instances(
            request(f"key-{n}", 1),
            provisioner=provisioner,
            ledger=ledger,
            guards=guards(),
            journal=journal,
            clock=lambda: 7,
        )

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(one, range(200)))
    ids = [i for r in results for i in r.instance_ids]
    assert len(ids) == len(set(ids)) == 200
    assert set(ids) == {i for live in adapter.live.values() for i in live}
    assert ledger.available == 0 and adapter.peak == 200
    with pytest.raises(ProvisionRefused) as over:
        one(200)
    assert over.value.code == "PROVISION_BUDGET_EXCEEDED"
    assert adapter.calls == 200


@pytest.mark.spec("ARBORA-FLEET-R024.5")
def test_replay_returns_the_same_receipt_and_holds_capacity_once(sdk: Any) -> None:
    adapter, ledger, _journal, call = _run(sdk, request("replay", 5))
    first, second = call(), call()
    assert first == second and adapter.calls == 1
    assert ledger.held_by("replay") == 5


@pytest.mark.spec("ARBORA-FLEET-R024.5")
def test_config_bound_and_default() -> None:
    assert max_concurrent_instances({}) == 200
    assert (
        max_concurrent_instances({"provisioning": {"max_concurrent_instances": 2000}})
        == 2000
    )
    for bad in (0, 2001, "5", True):
        with pytest.raises(ValueError):
            max_concurrent_instances(
                {"provisioning": {"max_concurrent_instances": bad}}
            )


def _agent_context(service: Any) -> Any:
    caller = SimpleNamespace(
        session=service_session("tenant:one", "graph:one"), tenant="tenant:one"
    )
    return SimpleNamespace(
        caller=caller,
        services={"a2a": service},
        operation_services=None,
        idempotency_key="run-key",
    )


class _Service:
    def __init__(self, authority: Any) -> None:
        self.authority = authority
        self.sent: list[Any] = []

    async def send_message(self, **kwargs: Any) -> dict[str, Any]:
        self.sent.append(kwargs)
        return SimpleNamespace(model_dump=lambda **_kw: {"id": "task-1"})


@pytest.mark.usefixtures("default_agent_registered")
@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R024.6")
async def test_agents_run_reaches_provisioner_and_consumer(sdk: Any) -> None:
    adapter = DisposableAdapter()
    ledger, journal = new_run(10)
    authority = WorkItemA2AAuthority(
        lambda: None, origins=RunOriginRegistry(), config=RuntimeConfig()
    )
    authority.provisioning = ProvisioningService(
        provisioner=make_provisioner(sdk, adapter),
        ledger=ledger,
        guards=guards(),
        journal=journal,
        clock=lambda: 3,
    )
    service = _Service(authority)
    op = OpSpec.model_construct(id="agents.run")
    params = {
        "message": {"role": "user", "metadata": {}},
        "provision": request("run-1", 2).model_dump(),
    }
    await handle_agent(_agent_context(service), params, op)
    (sent,) = service.sent
    meta = sent["message"]["metadata"]
    assert sorted(meta["provisioned_instances"]) == sorted(
        i for ids in adapter.live.values() for i in ids
    )
    assert len(meta["provisioned_instances"]) == 2
    assert meta["allocation_receipt_digest"]
    assert ledger.held_by("run-1") == 2 and journal.verify()


@pytest.mark.usefixtures("default_agent_registered")
@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R024.6")
async def test_agents_run_refuses_a_foreign_tenant_and_a_lagging_sdk() -> None:
    authority = WorkItemA2AAuthority(
        lambda: None, origins=RunOriginRegistry(), config=RuntimeConfig()
    )
    service = _Service(authority)
    op = OpSpec.model_construct(id="agents.run")
    body = {
        "message": {"role": "user"},
        "provision": request("k", 1, tenant="tenant:two").model_dump(),
    }
    with pytest.raises(OperationRefused) as foreign:
        await handle_agent(_agent_context(service), body, op)
    assert foreign.value.code == "PROVISION_GRANT_DENIED" and service.sent == []
    body["provision"] = request("k", 1).model_dump()
    with pytest.raises(RuntimeError, match="provisioning is unavailable"):
        await handle_agent(_agent_context(service), body, op)
    assert service.sent == []


@pytest.mark.spec("ARBORA-FLEET-R024.7")
def test_refusals_allocate_nothing(sdk: Any) -> None:
    cases: list[tuple[str, Any, dict[str, Any]]] = [
        ("PLATFORM_UNSUPPORTED", request("a", 1, os="plan9"), {}),
        ("PROVISION_GRANT_DENIED", request("b", 1), {"guards": guards(grant=False)}),
        ("PROVISION_DECISION_STALE", request("c", 1), {"guards": guards(fresh=False)}),
        ("PROVISION_BUDGET_EXCEEDED", request("d", 5, budget=2), {}),
    ]
    for code, req, extra in cases:
        adapter, ledger, journal, call = _run(sdk, req)
        with pytest.raises(ProvisionRefused) as refused:
            call(**extra)
        assert refused.value.code == code and code in REFUSALS
        assert adapter.calls == 0 and ledger.available == 200
        assert not journal._events and ledger.held_by(req.idempotency_key) == 0


@pytest.mark.spec("ARBORA-FLEET-R024.7")
def test_replay_with_another_body_refuses_and_keeps_the_first_hold(sdk: Any) -> None:
    adapter, ledger, _journal, call = _run(sdk, request("same", 2))
    call()
    with pytest.raises(ProvisionRefused) as changed:
        call(request("same", 2, image_digest="c" * 64))
    assert changed.value.code == "PROVISION_REPLAY_BODY_CHANGED"
    assert adapter.calls == 1 and ledger.held_by("same") == 2


@pytest.mark.spec("ARBORA-FLEET-R024.7")
def test_unknown_outcome_keeps_capacity_and_is_never_repeated(sdk: Any) -> None:
    adapter, ledger, _journal, call = _run(sdk, request("unk", 3), mode="timeout")
    with pytest.raises(ProvisionRefused) as unknown:
        call()
    assert unknown.value.code == "PROVISION_OUTCOME_UNKNOWN"
    allocation_id = unknown.value.details["allocation_id"]
    assert adapter.instances(allocation_id) and ledger.held_by("unk") == 3
    with pytest.raises(ProvisionRefused):
        call()
    assert adapter.calls == 1


@pytest.mark.spec("ARBORA-FLEET-R024.7")
def test_partial_failure_keeps_the_real_ids(sdk: Any) -> None:
    adapter, _ledger, _journal, call = _run(sdk, request("part", 4), mode="short")
    result = call()
    assert len(result.instance_ids) == 3 and result.shortfall == 1
    assert set(result.instance_ids) == {i for v in adapter.live.values() for i in v}


@pytest.mark.spec("ARBORA-FLEET-R024.8")
def test_counters_reconcile_with_runtime(sdk: Any) -> None:
    adapter, ledger, journal, call = _run(sdk, request("ev", 4))
    states = {}

    def observe(ids: tuple[str, ...]) -> dict[str, str]:
        states.update({ids[0]: "completed", ids[1]: "failed"})
        return states

    result = call(observe=observe)
    (event,) = [e for e in journal._events if e.kind == PROVISION_PROOF_KIND]
    assert event.subject_ref.startswith("provision:") and event.observed_at == 7
    assert result.outcomes[next(iter(states))] == "completed"
    assert sum(len(v) for v in adapter.live.values()) == len(result.instance_ids) == 4
    assert ledger.held_by("ev") == 4


@pytest.mark.spec("ARBORA-FLEET-R024.11")
def test_aggregation_binding_is_carried_into_the_proof(sdk: Any) -> None:
    ref = AggregationRef(
        operation="DecomposeAggregate", version="3", input_receipt_ids=("r1", "r2")
    )
    digests = []
    for aggregation in (None, ref):
        _adapter, _ledger, journal, call = _run(sdk, request("agg", 2))
        call(aggregation=aggregation)
        digests.append(journal._events[0].payload_digest)
    assert digests[0] != digests[1]
    with pytest.raises(ValueError):
        AggregationRef(operation="x", version="1", input_receipt_ids=())


@pytest.mark.spec("ARBORA-FLEET-R024.9")
@pytest.mark.spec("ARBORA-FLEET-R024.10")
def test_matrix_two_hundred_refusal_replay_interrupt(sdk: Any) -> None:
    adapter = DisposableAdapter()
    provisioner = make_provisioner(sdk, adapter)
    ledger, journal = new_run(200)
    for n, (os_, runtime) in enumerate(sorted(MATRIX)):
        provision_instances(
            request(f"row-{n}", 1, os=os_, runtime=runtime),
            provisioner=provisioner,
            ledger=ledger,
            guards=guards(),
            journal=journal,
            clock=lambda: 1,
        )
    with pytest.raises(ProvisionRefused):
        provision_instances(
            request("bad", 1, os="plan9"),
            provisioner=provisioner,
            ledger=ledger,
            guards=guards(),
            journal=journal,
            clock=lambda: 1,
        )
    calls = adapter.calls
    again = provision_instances(
        request("row-0", 1, os="linux", runtime="docker"),
        provisioner=provisioner,
        ledger=ledger,
        guards=guards(),
        journal=journal,
        clock=lambda: 1,
    )
    assert again.instance_ids and adapter.calls == calls
    interrupted = DisposableAdapter("timeout")
    with pytest.raises(ProvisionRefused) as lost:
        provision_instances(
            request("int", 197),
            provisioner=make_provisioner(sdk, interrupted),
            ledger=ledger,
            guards=guards(),
            journal=journal,
            clock=lambda: 1,
        )
    assert lost.value.code == "PROVISION_OUTCOME_UNKNOWN"
    assert ledger.held_by("int") == 197 and ledger.available == 0
