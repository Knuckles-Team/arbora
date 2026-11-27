"""The host composes the scale reconciler over WorkItems and the authority over the graph.

ARBORA-FLEET-R024.14 (ledger on engine WorkItems, reconciler composed) and
ARBORA-FLEET-R024.15 (authority snapshot read from graph nodes). The bindings come from
the host's own composition functions; the engine is a structural double of its WorkItem
and node-read methods.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import msgpack
import pytest

from arbora import work_items
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import fleet_scaling
from arbora.fleet.scaling import graph_authority
from arbora.fleet.scaling.graph_authority import INVENTORY, REVISION_LABEL
from arbora.fleet.scaling.scaling_authority import ScaleIntent
from arbora.fleet.scaling.work_item_ledger import WorkItemScaleIntentLedger
from arbora.mcp_server.served_invocation import (
    _with_provisioning_ports,
    _with_scale_ports,
)
from tests.api._loopback_engine import loopback  # noqa: F401
from tests.fleet.scaling._scale_builder import declare_scale_builder
from tests.fleet.scaling._work_item_engine import Session, engine
from tests.fleet.scaling.test_engine_scale_policy import PRINCIPAL, SCHEMA, Engine
from tests.fleet.scaling.test_scaling_authority_contract import INTENT as PLAN_INTENT
from tests.fleet.scaling.test_scaling_authority_contract import authority
from tests.fleet.scaling.test_scaling_intent_reconciler import INTENT, NOW
from tests.fleet.test_provisioning_ports_served import OPERATOR, FakeGateway
from tests.test_served_operation_api import served  # noqa: F401

OPS = {op.id: op for op in fleet_scaling.operations()}
SCALER = SimpleNamespace(
    effective_scopes=OPERATOR.effective_scopes, tenant="t1", principal=PRINCIPAL
)


class FakeGraph:
    """``GetNodesByLabel`` over classes of nodes, paged by the last node id."""

    def __init__(self) -> None:
        self.classes: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    def load(self, snapshot: Any, revision: int = 11) -> None:
        for field, label, _model in INVENTORY:
            self.classes[label] = [
                (f"{label}:{i}", record.model_dump(mode="json"))
                for i, record in enumerate(getattr(snapshot, field))
            ]
        self.classes[REVISION_LABEL] = [("rev", {"authority_revision": revision})]

    async def __call__(self, context: Any, method: str, params: dict[str, Any]) -> Any:
        assert method == "GetNodesByLabel"
        rows = sorted(self.classes.get(params["label"], []))
        rows = [
            r for r in rows if params.get("after") is None or r[0] > params["after"]
        ]
        page = rows[: params["limit"]]
        return [(nid, msgpack.packb(props, use_bin_type=True)) for nid, props in page]


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> FakeGraph:
    fake = FakeGraph()
    fake.load(authority())
    monkeypatch.setattr(graph_authority, "engine_send", fake)
    return fake


@pytest.fixture
def verdicts(monkeypatch: pytest.MonkeyPatch) -> Engine:
    """The engine's ``Decide`` answers (the question kind declared, as after the re-pin)."""

    verdict = Engine()

    async def graph(context: Any) -> str:
        return "graph:t1"

    declare_scale_builder(monkeypatch)
    monkeypatch.setattr("arbora.api.ops.engine_calls.engine_send", verdict)
    monkeypatch.setattr("arbora.api.invoke.bootstrap.caller_dispatch_graph", graph)
    return verdict


@pytest.fixture
def host(
    monkeypatch: pytest.MonkeyPatch, verdicts: Engine
) -> tuple[dict[str, Any], Any]:
    """The bindings the host composes, over a WorkItem engine double."""
    monkeypatch.setattr(work_items, "_session", Session)
    work_engine, fake = engine()
    gateway = FakeGateway()
    bindings = _with_scale_ports(
        _with_provisioning_ports(
            {"fleet_gateway": gateway, "task_plan_work_engine": work_engine}
        )
    )
    assert bindings is not None
    bindings["scale_reconciler"].clock = lambda: (
        NOW
    )  # the intent fixtures' controlled clock
    return dict(bindings), SimpleNamespace(gateway=gateway, work_items=fake)


def _context(bindings: dict[str, Any]) -> Any:
    return SimpleNamespace(
        caller=SCALER,
        services={"deploy_feature_schema": SCHEMA, **bindings},
        service_identity=True,
        owner="owner",
        registry_digest="digest",
    )


@pytest.mark.spec("ARBORA-FLEET-R024.14")
def test_the_host_composes_the_reconciler_over_the_workitem_ledger(
    host: tuple[dict[str, Any], Any],
) -> None:
    bindings, _ = host
    assert isinstance(bindings["scale_reconciler"].ledger, WorkItemScaleIntentLedger)
    own = object()
    kept = _with_scale_ports({"scale_reconciler": own, "scale_authority": own})
    assert kept is not None
    assert kept["scale_reconciler"] is own and kept["scale_authority"] is own
    assert _with_scale_ports(None) is None
    bare = _with_scale_ports({})
    assert bare is not None and "scale_reconciler" not in bare


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R024.14")
async def test_reconcile_runs_through_the_workitem_ledger_and_replays_once(
    host: tuple[dict[str, Any], Any],
) -> None:
    bindings, double = host
    context = _context(bindings)
    target = {"runtime_id": "deployment", "workload_id": "shop/api"}
    params = {
        "intent": {**INTENT, "target": {**INTENT["target"], **target}},
        "controller_id": "writer-1",
    }
    run = OPS["fleet.reconcile.run"]
    first = await fleet_scaling.handle_fleet_reconcile_run(context, params, run)
    again = await fleet_scaling.handle_fleet_reconcile_run(context, params, run)
    assert first["value"]["scaled"] is True and again["value"]["replayed"] is True
    assert len(double.gateway.calls) == 1
    assert "persist_intent" in double.work_items.events

    changed = {
        **params["intent"],
        "target": {**params["intent"]["target"], "target_uid": "other"},
    }
    with pytest.raises(OperationRefused) as fenced:
        await fleet_scaling.handle_fleet_reconcile_run(
            context, {**params, "intent": changed}, run
        )
    assert fenced.value.code == "FLEET_LEASE_FENCED"
    assert len(double.gateway.calls) == 1


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R024.15")
async def test_the_authority_snapshot_is_read_from_graph_nodes(
    graph: FakeGraph,
) -> None:
    snapshot = await graph_authority.GraphScaleAuthority().snapshot(object())
    assert snapshot == authority()


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R024.15")
async def test_scale_evaluate_admits_and_refuses_against_the_graph_authority(
    graph: FakeGraph, host: tuple[dict[str, Any], Any]
) -> None:
    bindings, _ = host
    context = _context(bindings)
    evaluate = OPS["fleet.scale.evaluate"]
    admitted = await fleet_scaling.handle_fleet_scale_evaluate(
        context, {"intent": ScaleIntent.model_validate(PLAN_INTENT)}, evaluate
    )
    assert admitted["admitted"] is True
    refused = await fleet_scaling.handle_fleet_scale_evaluate(
        context, {"intent": {**PLAN_INTENT, "desired_replicas": 9}}, evaluate
    )
    assert refused["admitted"] is False and "min/max" in refused["detail"]


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R024.15")
async def test_a_missing_or_invalid_inventory_refuses_unavailable(
    graph: FakeGraph,
) -> None:
    reader = graph_authority.GraphScaleAuthority()
    graph.classes["ScaleUnit"] = [("bad", {"unit_id": 1})]
    with pytest.raises(OperationRefused) as invalid:
        await reader.snapshot(object())
    assert invalid.value.code == "UNAVAILABLE" and "bad" in str(invalid.value.details)
    graph.load(authority())
    graph.classes[REVISION_LABEL] = []
    with pytest.raises(OperationRefused) as unrevised:
        await reader.snapshot(object())
    assert unrevised.value.code == "UNAVAILABLE"
    graph.load(authority())
    graph.classes["ScaleResourcePool"] = []
    with pytest.raises(OperationRefused) as inconsistent:
        await reader.snapshot(object())
    assert inconsistent.value.code == "UNAVAILABLE"


class ServedGateway(FakeGateway):
    """The gateway of a served call: the operator's own request carries no service identity."""

    async def call(
        self, caller: Any, server: str, tool: str, arguments: Any, **kw: Any
    ) -> Any:
        return await super().call(
            caller, server, tool, arguments, **{**kw, "service_identity": True}
        )


#: The stack the served tests drive: the engine WorkItem double and the fleet gateway.
STACK: dict[str, Any] = {}


def loopback_bindings() -> dict[str, Any]:
    """Extra bindings of the served app: the host composes the scale ports over them."""
    served_engine, fake = engine()
    gateway = ServedGateway()
    STACK.update(work_items=fake, gateway=gateway)
    return {"task_plan_work_engine": served_engine, "fleet_gateway": gateway}


def _decide(option: str) -> Any:
    from tests.fleet.scaling.test_engine_scale_policy import _record

    return lambda params, principal: _record(params["request"], option)


def _reconcile_params(
    execution_key: str, intent_id: str = "scale-intent-1"
) -> dict[str, Any]:
    from tests.fleet.scaling.test_scaling_intent_reconciler import intent

    # The host reconciler runs on the real clock, so the fence lies far in the future.
    fence = {**INTENT["fence"], "expires_at": datetime(2099, 1, 1, tzinfo=UTC)}
    target = {**INTENT["target"], "runtime_id": "deployment", "workload_id": "a/b"}
    record = intent(
        execution_key=execution_key, intent_id=intent_id, fence=fence, target=target
    )
    return {
        "intent": record.model_dump(mode="json"),
        "controller_id": "writer-1",
    }


@pytest.mark.spec("ARBORA-FLEET-R024.16")
def test_reconcile_served_through_invoke_acts_only_on_the_engine_verdict(
    loopback: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.ops import engine_calls as _engine

    monkeypatch.setattr(work_items, "_session", Session)
    declare_scale_builder(monkeypatch)
    verdict = {"option": "allow"}
    loopback.serve_unreleased(
        _engine,
        {
            "Decide": lambda params, principal: _decide(verdict["option"])(
                params, principal
            )
        },
    )
    loopback.serve({}, "mcp:delegate")

    allowed = loopback.call("fleet.reconcile.run", _reconcile_params("exec-1"), "k1")
    value = loopback.value(allowed)
    assert value["scaled"] is True and value["actuator_called"] is True
    gateway = STACK["gateway"]
    assert len(gateway.calls) == 1
    asked = [call for call in loopback.engine_calls if call[0] == "Decide"]
    assert len(asked) == 1
    assert asked[0][1]["request"]["question"]["kind"] == "scale_intent"

    replay = loopback.value(
        loopback.call("fleet.reconcile.run", _reconcile_params("exec-1"), "k2")
    )
    assert replay["replayed"] is True and len(gateway.calls) == 1
    assert len([c for c in loopback.engine_calls if c[0] == "Decide"]) == 1

    verdict["option"] = "refuse:SCALE_POOL_HEADROOM_EXCEEDED"
    denied = loopback.value(
        loopback.call(
            "fleet.reconcile.run", _reconcile_params("exec-2", "scale-intent-2"), "k3"
        )
    )
    assert denied["state"] == "denied" and denied["actuator_called"] is False
    assert len(gateway.calls) == 1
    committed = [e for e in STACK["work_items"].events if e.startswith("commit:")]
    assert committed == ["commit:succeeded"]
