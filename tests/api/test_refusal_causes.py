"""Refusal causes are logged server-side; replay owners are declared (ARBORA-OPS-R009.2, R009.3)."""

from __future__ import annotations

import logging
import sys
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from arbora.api.errors import to_envelope
from arbora.api.invoke import pipeline


@dataclass
class _Refusal:
    """A pipeline refusal (the OpErrorLike shape the envelope projects)."""

    code: str
    details: Mapping[str, Any] = field(default_factory=dict)


@pytest.mark.spec("ARBORA-OPS-R009.2")
def test_refusal_cause_is_logged_with_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    refusal = _Refusal("UNAVAILABLE", {"reason": "durable effect owner unavailable"})
    with caplog.at_level(logging.WARNING, logger="arbora.api.errors"):
        status, envelope = to_envelope(
            refusal, op="demo.op", request_id="req-123", registry_digest="d" * 64
        )
    assert status == 503
    assert envelope["error"]["request_id"] == "req-123"
    assert "durable effect owner" not in str(envelope)
    logged = caplog.text
    assert "demo.op" in logged and "req-123" in logged
    assert "durable effect owner unavailable" in logged


@pytest.mark.spec("ARBORA-OPS-R009.3")
def test_ops_modules_declare_replay_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    owners = pipeline._declared_owners()
    assert owners["agents.run"] == "arbora.a2a.authority:WorkItemA2AAuthority"
    module = types.ModuleType("arbora_test_declares_owner")
    module.NATIVE_IDEMPOTENCY = {"demo.write": "demo:Owner"}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    declared = pipeline._declared_owners(["arbora.api.ops.agents", module.__name__])
    assert declared["demo.write"] == "demo:Owner" and "agents.run" in declared


@pytest.mark.spec("ARBORA-OPS-R009.2")
def test_provider_schema_refusal_cause_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from arbora.api.invoke.steps import _validate_provider
    from arbora.api.registry import EgSchemaRef

    def refuse(_model: Any, _params: Any) -> Any:
        raise ValueError("records.0.provenance.tool_schema_sha256 is required")

    def assert_tenant(_model: Any, params: Any) -> Any:
        return {"nested": {"tenant": "other"}}

    model = EgSchemaRef(path="method.request.json#/methods/SourceIngest")
    with caplog.at_level(logging.WARNING, logger="arbora.api.invoke.steps"):
        refused = _validate_provider(model, {}, refuse)
    assert getattr(refused, "code", None) == "INVALID_ARGUMENT"
    assert "tool_schema_sha256 is required" in caplog.text
    asserted = _validate_provider(model, {}, assert_tenant)
    assert getattr(asserted, "details", None) == {"field": "nested.tenant"}


@pytest.mark.spec("ARBORA-OPS-R009.2")
async def test_engine_refusal_detail_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from types import SimpleNamespace

    from epistemic_graph import EngineResponseError

    from arbora.api.errors import EngineRefusal
    from arbora.api.invoke.executor import BoundOperationRuntime

    async def refuse(*_args: Any) -> Any:
        raise EngineResponseError("INVALID_ARGUMENT", "records[0].stream is empty")

    runtime = object.__new__(BoundOperationRuntime)
    runtime._eg_dispatch = refuse  # type: ignore[attr-defined]
    op = SimpleNamespace(
        id="eg.ingestion.SourceIngest", binding=SimpleNamespace(kind="eg")
    )
    context = SimpleNamespace(caller=SimpleNamespace(request_id="req-9"))
    with caplog.at_level(logging.WARNING, logger="arbora.api.invoke.executor"):
        with pytest.raises(EngineRefusal):
            await runtime.dispatch(op, {}, context)  # type: ignore[arg-type]
    assert "eg.ingestion.SourceIngest" in caplog.text and "req-9" in caplog.text
    assert "records[0].stream is empty" in caplog.text
