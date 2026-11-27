"""The operator audit port behind pin-lag (ARBORA-OPS-R052.5).

The engine serves the operator-only chain (``AuditList``) on ``main`` only.
Until the pinned release carries it, the port records nothing, the read refuses
``UNAVAILABLE`` naming the missing member and ``ops.doctor`` says so. With the chain
declared the same port writes and reads through the engine's operator chain.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import ops as ops_admin
from arbora.api.ops.operator_audit import EngineOperatorAudit
from tests.api.test_ops_admin_ops import _context, _FakeOpsAdminStore
from tests.question_kinds import declare_question_kinds

_DOCTOR = {op.id: op for op in ops_admin.operations()}["ops.doctor"]
INACTIVE = "operator_audit: inactive (engine lacks AuditList)"


def _pin_without_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    import medulla.generated as generated

    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset(generated.METHOD_IDS) - {"AuditList"}
    )


def _pin_with_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    import medulla.generated as generated

    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset(generated.METHOD_IDS) | {"AuditList"}
    )


def _sent(monkeypatch: pytest.MonkeyPatch, answer: Any = None) -> list[tuple[str, Any]]:
    import arbora.api.ops._engine as engine

    sent: list[tuple[str, Any]] = []

    async def send(context: Any, method: str, params: dict[str, Any], **_: Any) -> Any:
        sent.append((method, params))
        return answer

    monkeypatch.setattr(engine, "engine_send", send)
    return sent


def _call_context() -> Any:
    return SimpleNamespace(surface="http", caller=SimpleNamespace(request_id="req-9"))


@pytest.mark.spec("ARBORA-OPS-R052.5")
async def test_doctor_reports_the_operator_audit_inactive_until_the_chain_is_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declare_question_kinds(
        monkeypatch, "autonomy_verdict", "tool_relevance", "team_coordination"
    )
    _pin_without_chain(monkeypatch)
    report = await ops_admin.handle_ops_doctor(
        _context(store=_FakeOpsAdminStore()), {}, _DOCTOR
    )
    assert report["value"]["checks"]["operator_audit"] == INACTIVE

    _pin_with_chain(monkeypatch)
    report = await ops_admin.handle_ops_doctor(
        _context(store=_FakeOpsAdminStore()), {}, _DOCTOR
    )
    assert report["value"]["checks"]["operator_audit"] == "operator_audit: active"


@pytest.mark.spec("ARBORA-OPS-R052.5")
async def test_inactive_port_records_nothing_and_the_read_names_the_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_without_chain(monkeypatch)
    sent = _sent(monkeypatch)
    port = EngineOperatorAudit()
    await port.record_withheld(_call_context(), withheld=3, request_id="req-9")
    assert sent == []
    with pytest.raises(OperationRefused) as refused:
        await port.read(_call_context(), request_id="req-9")
    assert refused.value.code == "UNAVAILABLE"
    assert "AuditList" in str(refused.value.details)


@pytest.mark.spec("ARBORA-OPS-R052.5")
async def test_active_port_writes_and_reads_the_engine_operator_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_with_chain(monkeypatch)
    page = {
        "graph": "g",
        "records": [{"seq": 4, "entry_sha256": "a" * 64, "event_line": "line"}],
        "next_after_seq": None,
    }
    sent = _sent(monkeypatch, page)
    port = EngineOperatorAudit()
    await port.record_withheld(_call_context(), withheld=3, request_id="req-9")
    ((method, params),) = sent
    assert method == "AuditAppend"
    assert params["audit_class"] == "operator"
    assert params["evidence"] == {"withheld_count": "3"}
    assert params["request_id"] == "req-9" and params["surface"] == "http"
    sent.clear()
    record = await port.read(_call_context(), request_id="req-9")
    assert sent == [
        ("AuditList", {"chain": "operator", "request_id": "req-9", "limit": 1})
    ]
    assert record == {
        "request_id": "req-9",
        "seq": 4,
        "entry_sha256": "a" * 64,
        "event_line": "line",
    }
    sent.clear()
    _sent(monkeypatch, {"graph": "g", "records": [], "next_after_seq": None})
    assert await port.read(_call_context(), request_id="req-none") is None


@pytest.mark.spec("ARBORA-OPS-R052.5")
def test_the_served_host_composes_the_operator_audit_port() -> None:
    from arbora.mcp_server.served_invocation import _with_operator_audit

    composed = _with_operator_audit({})
    assert isinstance(composed["operator_audit"], EngineOperatorAudit)
    own = object()
    assert _with_operator_audit({"operator_audit": own})["operator_audit"] is own
    assert _with_operator_audit(None) is None
