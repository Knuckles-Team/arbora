"""ARBORA-FLEET-R083.5: ``find`` serves the engine's order and degrades to the floor.

The ``find`` verb runs through the real invoke pipeline and the host's one ranking
port, bound to an in-process loopback engine that answers ``Decide`` the way
medulla's ``RankCatalog`` entries do. With the engine stopped, ``find`` answers the
floor order, says so, and never fails for want of a ranking.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest

from arbora.api.invoke import invoke
from arbora.api.mcp.resolve import IntentResolver
from arbora.api.mcp.verbs import dispatch_verb
from arbora.api.ops import activity as activity_ops
from arbora.api.policy.eunomia import PolicyGate
from arbora.api.registry import Registry
from arbora.fleet.ranking import HostRanking, engine_ask
from arbora.mcp_server.serving import MCPProjection
from arbora.observability.state import SubsystemWindows
from tests._support import unwrap_decide
from tests.api._support import make_verified_caller
from tests.api.test_activity_ops_served import _services

_make = make_verified_caller("fleet:read")


def caller() -> Any:
    return _make(effective_scopes=frozenset({"fleet:read", "mcp:discover"}))


PRINCIPAL = "service:arbora"
TENANT = "tenant:one"


class LoopbackEngine:
    """Answers ``Decide`` for the rank catalog; ``stopped`` refuses every call."""

    def __init__(self, order: Any) -> None:
        self.order = order
        self.stopped = False
        self.requests: list[dict[str, Any]] = []

    async def send(self, method: str, request: dict[str, Any], tenant: str) -> Any:
        if self.stopped:
            raise ConnectionError("engine stopped")
        assert method == "Decide" and tenant == TENANT
        request = unwrap_decide(request)
        self.requests.append(request)
        ids = [o["option_id"] for o in request["candidates"]["options"]]
        digest = hashlib.sha256(PRINCIPAL.encode()).hexdigest()
        return {
            "records": [
                {
                    "caller_principal": f"principal:sha256:{digest}",
                    "tenant_id": request["tenant_id"],
                    "question": request["question"],
                    "inputs": {"params": request["params"]},
                    "record_id": "record:one",
                    "record_digest": "digest:one",
                    "outcome": {
                        "outcome": "advisory",
                        "scores": [{"option_id": i} for i in self.order(ids)],
                    },
                }
            ]
        }


def _pinned(_tenant: str) -> dict[str, Any]:
    return {"component_id": "decide.schema.catalog.operation_rank"}


def _projection(ranking: HostRanking) -> MCPProjection:
    services = _services(None)
    registry = Registry(activity_ops.operations())
    return MCPProjection(
        registry,
        services,
        IntentResolver(ranking=ranking),
        lambda: caller(),
        PolicyGate("none"),
        invoke,
        windows=SubsystemWindows(),
    )


def _find(ranking: HostRanking) -> dict[str, Any]:
    return asyncio.run(
        dispatch_verb("find", _projection(ranking), intent="city activity trace")
    )


def _bound(engine: LoopbackEngine) -> HostRanking:
    port = HostRanking()
    port.bind(
        engine_ask(
            engine.send,
            tenant=lambda: TENANT,
            feature_schema=_pinned,
            principal=lambda: PRINCIPAL,
        )
    )
    return port


@pytest.mark.spec("ARBORA-FLEET-R083.5")
def test_find_uses_the_engine_order_and_degrades_to_the_floor() -> None:
    engine = LoopbackEngine(order=lambda ids: list(reversed(ids)))
    port = _bound(engine)

    served = _find(port)
    assert served["ok"] is True, served
    assert served["result"]["ranking"] == "medulla"
    assert "ranking_reason" not in served["result"]
    sent = engine.requests[0]
    assert sent["question"]["question_id"] == "catalog.operation_rank"
    assert sent["candidates"]["source"] == "rank_catalog"
    # Texts only: the request carries no caller score, rank or threshold.
    assert all(not o["numbers"] for o in sent["candidates"]["options"])
    ids = [i["op"] for i in served["result"]["items"]]
    assert ids == sorted(ids, reverse=True)

    engine.stopped = True
    degraded = _find(_bound(engine))
    assert degraded["ok"] is True, degraded
    assert degraded["result"]["ranking"] == "floor"
    assert degraded["result"]["ranking_reason"] == "decision_unavailable"
    assert degraded["result"]["items"]


@pytest.mark.spec("ARBORA-FLEET-R083.5")
def test_an_unbound_port_and_a_missing_pin_answer_the_floor() -> None:
    unbound = _find(HostRanking())
    assert unbound["result"]["ranking"] == "floor"
    assert unbound["result"]["ranking_reason"] == "decision_unavailable"

    engine = LoopbackEngine(order=lambda ids: ids)
    port = HostRanking()
    port.bind(
        engine_ask(
            engine.send,
            tenant=lambda: TENANT,
            feature_schema=lambda _t: {"component_id": "decide.schema.other"},
            principal=lambda: PRINCIPAL,
        )
    )
    answer = _find(port)
    assert answer["result"]["ranking"] == "floor"
    assert engine.requests == []  # no question is sent without its own pin


@pytest.mark.spec("ARBORA-FLEET-R083.5")
def test_the_ranking_counter_records_the_source() -> None:
    from arbora.observability import gateway_metrics
    from arbora.observability.state import Metrics

    if not gateway_metrics.PROMETHEUS_AVAILABLE:
        pytest.skip("the metrics extra is not installed")
    engine = LoopbackEngine(order=lambda ids: ids)
    _find(_bound(engine))
    body, _ = gateway_metrics.render_metrics(Metrics())
    assert b'arbora_ranking_total{source="medulla"}' in body


@pytest.mark.spec("ARBORA-FLEET-R083.5")
def test_the_host_binds_its_fleet_port_to_the_served_engine_sender() -> None:
    from types import SimpleNamespace

    from arbora.mcp_server.host_context import bind_ranking, build_host_context

    host = build_host_context()
    bind_ranking(host, SimpleNamespace(engine_send=None))
    assert not host.fleet.ranking.configured  # no engine endpoint: the floor answers

    engine = LoopbackEngine(order=lambda ids: list(reversed(ids)))
    bind_ranking(
        host,
        SimpleNamespace(
            engine_send=engine.send,
            decision_feature_schema=_pinned,
            decision_principal=lambda: PRINCIPAL,
            decision_tenant=lambda: TENANT,
        ),
    )
    assert host.fleet.ranking.configured
    served = _find(host.fleet.ranking)
    assert served["result"]["ranking"] == "medulla"
