"""The floor order is never silent (ARBORA-FLEET-R083.5).

``find`` reports ``ranking: floor`` until the engine publishes the ranking
questions' feature schemas. ``/health/ready`` and ``ops.doctor`` say so too.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.fleet.ranking import QUESTION_IDS
from arbora.mcp_server.host_context import bind_ranking, build_host_context
from arbora.observability import runtime_health
from tests.api.test_ops_admin_ops import _context, _FakeOpsAdminStore, ops_admin

_OPS = {op.id: op for op in ops_admin.operations()}
UNPUBLISHED = "ranking: floor (engine schema unpublished)"


def _invocation(schema: Any) -> Any:
    async def send(*_a: Any, **_k: Any) -> Any:
        raise ConnectionError("not reached")

    return SimpleNamespace(
        engine_send=send,
        decision_feature_schema=schema,
        decision_principal=lambda: "service:arbora",
        decision_tenant=lambda: "tenant:one",
    )


def _published(_tenant: str) -> dict[str, Any]:
    return {"component_id": "decide.schema.catalog.operation_rank"}


@pytest.fixture(autouse=True)
def _probes() -> Any:
    runtime_health.reset_health_probes()
    yield
    runtime_health.reset_health_probes()


def _check() -> dict[str, Any]:
    report = runtime_health.collect_health()
    return next(c for c in report["checks"] if c["name"] == "ranking")


async def _doctor_checks() -> dict[str, Any]:
    result = await ops_admin.handle_ops_doctor(
        _context(store=_FakeOpsAdminStore()), {}, _OPS["ops.doctor"]
    )
    return dict(result["value"]["checks"])


@pytest.mark.spec("ARBORA-FLEET-R083.5")
async def test_unpublished_schema_is_named_in_ready_and_doctor() -> None:
    host = build_host_context()
    bind_ranking(host, _invocation(lambda _t: {"component_id": "decide.schema.other"}))
    check = _check()
    assert check["status"] == "degraded"
    assert check["reason"] == "engine schema unpublished"
    assert check["detail"]["summary"] == UNPUBLISHED
    assert "ranking" not in runtime_health.failing_checks(
        runtime_health.collect_health()
    )  # the floor is informational, never unready
    assert (await _doctor_checks())["ranking"] == UNPUBLISHED


@pytest.mark.spec("ARBORA-FLEET-R083.5")
async def test_an_unbound_engine_names_the_floor_reason() -> None:
    host = build_host_context()
    bind_ranking(host, SimpleNamespace(engine_send=None))
    assert _check()["detail"]["summary"] == "ranking: floor (engine unavailable)"
    assert (await _doctor_checks())["ranking"] == "ranking: floor (engine unavailable)"


@pytest.mark.spec("ARBORA-FLEET-R083.5")
async def test_published_schemas_report_the_engine_order() -> None:
    assert "catalog.operation_rank" in QUESTION_IDS
    host = build_host_context()
    bind_ranking(host, _invocation(_published))
    assert _check()["status"] == "ok"
    assert _check()["detail"]["summary"] == "ranking: medulla"
    assert (await _doctor_checks())["ranking"] == "ranking: medulla"
