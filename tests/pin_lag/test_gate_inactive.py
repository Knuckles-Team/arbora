"""An inactive engine-backed gate is named, never silent (ARBORA-OPS-R866, R867, R877)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.ops import ops as ops_admin
from arbora.api.ops.ops import DoctorReport
from arbora.mcp_server.gate_audit import gate_inactive_event, gate_inactive_startup
from arbora.pin_lag import gates
from arbora.security.session import use_session
from tests.a2a.test_operation_bridge import session
from tests.question_kinds import declare_autonomy_shapes

_LINES = [
    "gate_inactive: autonomy (engine lacks autonomy_verdict)",
    "gate_inactive: tool_relevance (engine lacks tool_relevance)",
    "gate_inactive: team_coordination (engine lacks team_coordination)",
]


def _served(monkeypatch: pytest.MonkeyPatch, *kinds: str) -> None:
    """A pinned contract that names exactly ``kinds``."""
    monkeypatch.setattr(gates, "question_kind_served", lambda kind: kind in kinds)
    declare_autonomy_shapes(monkeypatch)


@pytest.mark.spec("ARBORA-OPS-R867")
def test_every_gate_without_its_question_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _served(monkeypatch)
    assert gates.inactive_gate_lines() == _LINES
    _served(monkeypatch, "tool_relevance")
    assert gates.inactive_gate_lines() == [_LINES[0], _LINES[2]]
    _served(monkeypatch, "autonomy_verdict", "tool_relevance", "team_coordination")
    assert gates.inactive_gate_lines() == []


@pytest.mark.spec("ARBORA-OPS-R867")
def test_ready_reports_the_inactive_gates_and_stays_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.testclient import TestClient

    from arbora.mcp_server import runtime
    from arbora.observability import runtime_health

    async def healthy() -> dict[str, Any]:
        return {"checks": [{"name": "engine", "status": "healthy"}]}

    monkeypatch.setattr(runtime_health, "collect_health_async", healthy)
    _served(monkeypatch, "tool_relevance")
    _args, mcp, _middlewares = runtime._build_server(bootstrap=False)
    client = TestClient(mcp.http_app())
    body = client.get("/health/ready").json()
    # Readiness is never gated by a missing question: refusing every call would halt.
    assert body["status"] == "ready"
    assert body["gate_inactive"] == [_LINES[0], _LINES[2]]
    _served(monkeypatch, "autonomy_verdict", "tool_relevance", "team_coordination")
    assert "gate_inactive" not in client.get("/health/ready").json()


class _Store:
    async def doctor(self, *, tenant: str) -> DoctorReport:
        return DoctorReport(tenant=tenant, status="healthy", checks={"disk": "ok"})


@pytest.mark.spec("ARBORA-OPS-R867")
async def test_ops_doctor_reports_the_inactive_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _served(monkeypatch, "autonomy_verdict")
    context = SimpleNamespace(
        caller=SimpleNamespace(tenant="tenant-a", principal="p"),
        services={"ops_admin_store": _Store()},
        idempotency_key=None,
    )
    op = {o.id: o for o in ops_admin.operations()}["ops.doctor"]
    report = (await ops_admin.handle_ops_doctor(context, {}, op))["value"]
    assert report["checks"]["disk"] == "ok"
    assert report["checks"]["gate.tool_relevance"] == _LINES[1]
    assert report["checks"]["gate.team_coordination"] == _LINES[2]
    assert "gate.autonomy" not in report["checks"]


class _Adapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def preflight(self, event: Any, audit_class: Any, caller: Any) -> str:
        self.calls.append(("preflight", dict(event)))
        return "ref-1"

    async def write(self, event: Any, audit_class: Any, caller: Any) -> None:
        self.calls.append(("write", dict(event)))


@pytest.mark.spec("ARBORA-OPS-R867")
async def test_start_records_one_audit_event_naming_the_inactive_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _served(monkeypatch, "team_coordination")
    adapter = _Adapter()
    who = session(frozenset())
    with use_session(who):
        await gate_inactive_startup(adapter, lambda: who)()
    assert [name for name, _ in adapter.calls] == ["preflight", "write"]
    event = adapter.calls[0][1]
    assert event["op"] == "host.gate_inactive:autonomy,tool_relevance"
    assert event["surface"] == "console"
    assert adapter.calls[1][1]["result_status"] == "OK"
    assert adapter.calls[1][1]["audit_ref"] == "ref-1"


@pytest.mark.spec("ARBORA-OPS-R867")
async def test_no_audit_event_once_every_gate_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _served(monkeypatch, "autonomy_verdict", "tool_relevance", "team_coordination")
    adapter = _Adapter()
    who = session(frozenset())
    await gate_inactive_startup(adapter, lambda: who)()
    assert adapter.calls == []
    assert gate_inactive_event("t", "p", "r") == {}


@pytest.mark.spec("ARBORA-OPS-R867")
def test_an_engine_naming_the_question_but_not_the_typed_shapes_keeps_the_gate_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The question without ``AutonomyRequest`` is still inactive, naming the missing shape."""
    from medulla.generated import models

    monkeypatch.delattr(models, "AutonomyRequest", raising=False)
    monkeypatch.setattr(
        gates, "question_kind_served", lambda kind: kind == "autonomy_verdict"
    )
    assert not gates.gate_served("autonomy")
    assert gates.gate_inactive_line("autonomy") == (
        "gate_inactive: autonomy (engine lacks AutonomyRequest)"
    )
    declare_autonomy_shapes(monkeypatch)
    assert gates.gate_served("autonomy")
