"""ARBORA-FLEET-R015.1: a failed fleet child call is classified by its real cause."""

from __future__ import annotations

import inspect
import logging

import mcp.types
import pytest

from arbora.api.errors import FleetRefusal
from arbora.api.invoke import pipeline
from arbora.fleet import gateway_ops
from arbora.fleet import multiplexer as mux_module
from arbora.fleet.child_resilience import MCPError

pytestmark = pytest.mark.spec("ARBORA-FLEET-R015.1")


def _mcp_error(code: int, message: str) -> BaseException:
    if "code" in inspect.signature(MCPError.__init__).parameters:
        return MCPError(code, message)
    return MCPError(mcp.types.ErrorData(code=code, message=message))


def _group(*leaves: Exception) -> ExceptionGroup:
    return ExceptionGroup("unhandled errors in a TaskGroup", list(leaves))


def test_child_refusal_inside_task_group_keeps_child_message(caplog):
    caplog.set_level(logging.WARNING, logger="mcp_multiplexer")
    exc = _group(
        _mcp_error(
            -32603, "Forbidden: You don't have permission to access this resource."
        )
    )

    result = mux_module._child_failure_result("qbittorrent-mcp", "get_version", exc)

    assert result.isError is True
    assert gateway_ops._child_error_code(result) == "CHILD_REFUSED"
    assert "Forbidden" in gateway_ops._child_error_text(result)
    assert "TaskGroup" not in gateway_ops._child_error_text(result)
    assert "server=qbittorrent-mcp" in caplog.text
    assert "Forbidden" in caplog.text


def test_transport_leaf_is_unavailable_not_refused():
    exc = _group(ConnectionRefusedError("connection refused"))

    result = mux_module._child_failure_result("langfuse-mcp", "health", exc)

    assert gateway_ops._child_error_code(result) == "UNAVAILABLE"


def test_dead_session_signature_is_unavailable():
    exc = _group(_mcp_error(32600, "Session terminated"))

    result = mux_module._child_failure_result("langfuse-mcp", "health", exc)

    assert gateway_ops._child_error_code(result) == "UNAVAILABLE"


def test_credential_text_is_redacted_from_log_and_result(caplog):
    caplog.set_level(logging.WARNING, logger="mcp_multiplexer")
    exc = _group(_mcp_error(-32603, "login failed password=hunter2-synthetic"))

    result = mux_module._child_failure_result("qbittorrent-mcp", "get_version", exc)

    assert "hunter2-synthetic" not in caplog.text
    assert "hunter2-synthetic" not in gateway_ops._child_error_text(result)


def test_unknown_cause_stays_opaque_but_logs_leaf_class(caplog):
    caplog.set_level(logging.WARNING, logger="mcp_multiplexer")
    exc = _group(ValueError("internal detail"))

    result = mux_module._child_failure_result("x-mcp", "t", exc)

    assert gateway_ops._child_error_code(result) == "CHILD_REFUSED"
    assert "ValueError" in caplog.text


async def test_unmountable_child_answers_unavailable_without_dispatch(monkeypatch):
    mux = mux_module.MCPMultiplexer.__new__(mux_module.MCPMultiplexer)
    monkeypatch.setattr(mux_module, "_require_fleet_capability", lambda *a, **k: None)
    monkeypatch.setattr(type(mux), "load_catalog", lambda self: {})

    result = await mux._call_service_child_once(
        "langfuse-mcp", "health", {}, timeout=None
    )

    assert gateway_ops._child_error_code(result) == "UNAVAILABLE"


@pytest.mark.parametrize(
    ("leaf", "code", "source"),
    [
        (
            FleetRefusal("CHILD_REFUSED", "s", "t", detail="nope"),
            "CHILD_REFUSED",
            "fleet",
        ),
    ],
)
def test_pipeline_unwraps_a_single_grouped_refusal(leaf, code, source):
    refused = pipeline._grouped_refusal(_group(leaf))

    assert refused is not None
    assert refused.code == code
    assert refused.source == source
    assert refused.details["detail"] == "nope"


def test_pipeline_keeps_unsettled_for_an_unknown_grouped_cause():
    assert pipeline._grouped_refusal(_group(ValueError("x"))) is None
    assert pipeline._grouped_refusal(ValueError("x")) is None
    assert (
        pipeline._cause_classes(_group(OSError("a"), ValueError("b")))
        == "OSError,ValueError"
    )
