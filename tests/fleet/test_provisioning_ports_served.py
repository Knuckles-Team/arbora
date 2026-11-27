"""The rollback dispatcher and scale actuator act through one governed fleet call.

The host composes both ports (ARBORA-FLEET-R024.12, R024.13). The connector is a
structural double of the container-manager connector's served tools
(``cm_k8s_workloads`` and ``cm_multi_context``) behind the request's fleet gateway.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import fleet_deploy_watch, fleet_scaling
from arbora.fleet.scaling.scaling_intent_reconciler import (
    AllowScalePolicy,
    MemoryScaleIntentLedger,
    ScaleIntentReconciler,
)
from arbora.mcp_server.served_invocation import _with_provisioning_ports
from tests.fleet.scaling.test_scaling_intent_reconciler import INTENT, NOW, TARGET

OPERATOR = SimpleNamespace(effective_scopes=frozenset({"mcp:delegate"}))


class FakeGateway:
    """Records each governed call; answers like the connector's tools."""

    def __init__(self, answer: Any = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.answer = answer

    async def effect(self, op: Any, params: Any, caller: Any) -> Any:
        return SimpleNamespace(effect=op.effect)

    async def call(
        self, caller: Any, server: str, tool: str, arguments: Any, **kw: Any
    ) -> Any:
        assert kw["service_identity"] is True
        self.calls.append((server, tool, dict(arguments)))
        if self.answer is not None:
            return self.answer
        return {"replicas": arguments.get("replicas", 0)}


def _context(gateway: FakeGateway, **services: Any) -> Any:
    bindings = _with_provisioning_ports({"fleet_gateway": gateway, **services})
    assert bindings is not None
    return SimpleNamespace(
        caller=OPERATOR,
        services=dict(bindings),
        service_identity=True,
        owner="owner",
        registry_digest="digest",
    )


def _dispatch_op() -> Any:
    return next(
        op
        for op in fleet_deploy_watch.operations()
        if op.id == "fleet.deploy_watch.dispatch"
    )


DOWN = {"service": "shop/api", "probes": ({"status": "down", "detail": "503"},)}


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R024.12")
async def test_a_failed_deploy_watch_dispatches_rollout_undo_through_fleet_call() -> (
    None
):
    gateway = FakeGateway({"result": "rolled back"})
    context = _context(gateway)
    done = await fleet_deploy_watch.handle_fleet_deploy_watch_dispatch(
        context, DOWN, _dispatch_op()
    )
    assert done["dispatched"] is True
    assert gateway.calls == [
        (
            "container-manager-mcp",
            "cm_k8s_workloads",
            {
                "action": "rollout_undo",
                "resource_type": "deployment",
                "resource_name": "api",
                "namespace": "shop",
            },
        )
    ]
    quiet = await fleet_deploy_watch.handle_fleet_deploy_watch_dispatch(
        context, {"service": "shop/api", "probes": ()}, _dispatch_op()
    )
    assert quiet["dispatched"] is False and len(gateway.calls) == 1

    refusing = _context(FakeGateway({"error": "denied"}))
    with pytest.raises(OperationRefused) as caught:
        await fleet_deploy_watch.handle_fleet_deploy_watch_dispatch(
            refusing, DOWN, _dispatch_op()
        )
    assert caught.value.code == "UPSTREAM_FAILED"


def _reconcile(context: Any, ledger: MemoryScaleIntentLedger, **update: Any) -> Any:
    reconciler = ScaleIntentReconciler(
        ledger,
        context.services["scale_actuator"],
        policy=AllowScalePolicy(),
        clock=lambda: NOW,
    )
    context.services["scale_reconciler"] = reconciler
    op = next(o for o in fleet_scaling.operations() if o.id == "fleet.reconcile.run")
    params = {
        "intent": {**INTENT, "target": {**TARGET, **update}},
        "controller_id": "writer-1",
    }
    return fleet_scaling.handle_fleet_reconcile_run(context, params, op)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R024.13")
async def test_the_scale_actuator_scales_through_fleet_call_and_replays_once() -> None:
    gateway = FakeGateway()
    context = _context(gateway)
    ledger = MemoryScaleIntentLedger()
    target = {"runtime_id": "deployment", "workload_id": "shop/api"}
    first = await _reconcile(context, ledger, **target)
    assert first["value"]["scaled"] is True
    assert gateway.calls == [
        (
            "container-manager-mcp",
            "cm_multi_context",
            {
                "action": "scale_deployment",
                "name": "api",
                "namespace": "shop",
                "replicas": 3,
                "context": "cluster-1",
            },
        )
    ]
    again = await _reconcile(context, ledger, **target)
    assert again["value"]["replayed"] is True and len(gateway.calls) == 1

    sts = _context(FakeGateway())
    await _reconcile(
        sts,
        MemoryScaleIntentLedger(),
        runtime_id="statefulset",
        workload_id="data/db",
    )
    assert sts.services["fleet_gateway"].calls[0][1:] == (
        "cm_k8s_workloads",
        {
            "action": "scale_statefulset",
            "name": "db",
            "namespace": "data",
            "replicas": 3,
        },
    )


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R024.13")
async def test_the_scale_actuator_never_claims_what_the_connector_did_not_report() -> (
    None
):
    target = {"runtime_id": "deployment", "workload_id": "shop/api"}
    refused = await _reconcile(
        _context(FakeGateway({"error": "denied"})), MemoryScaleIntentLedger(), **target
    )
    assert refused["value"]["scaled"] is False
    silent = await _reconcile(
        _context(FakeGateway({"result": "ok"})), MemoryScaleIntentLedger(), **target
    )
    assert silent["value"]["scaled"] is False
    unknown = await _reconcile(
        _context(FakeGateway()), MemoryScaleIntentLedger(), runtime_id="mystery"
    )
    assert unknown["value"]["scaled"] is False


def test_the_host_keeps_ports_it_was_given_and_binds_none_without_bindings() -> None:
    assert _with_provisioning_ports(None) is None
    own = object()
    bound = _with_provisioning_ports({"deploy_rollback": own})
    assert bound is not None and bound["deploy_rollback"] is own
