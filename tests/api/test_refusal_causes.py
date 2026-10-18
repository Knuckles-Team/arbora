"""Refusal causes are logged server-side; replay owners are declared (ARBORA-OPS-R009.2, R009.3)."""

from __future__ import annotations

import logging
import sys
import types

import pytest

from arbora.api.errors import to_envelope
from arbora.api.invoke import pipeline
from arbora.api.invoke.pipeline import OpError


@pytest.mark.spec("ARBORA-OPS-R009.2")
def test_refusal_cause_is_logged_with_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    refusal = OpError("UNAVAILABLE", {"reason": "durable effect owner unavailable"})
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
