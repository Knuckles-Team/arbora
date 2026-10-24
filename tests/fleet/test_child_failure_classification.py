"""ARBORA-FLEET-R015.1: a failed fleet child call is classified by its real cause."""

from __future__ import annotations

import inspect
import logging

import mcp.types
import pytest
from fastmcp.exceptions import ToolError

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


@pytest.mark.parametrize("transport_first", [False, True])
def test_transport_precedes_nested_refusal_and_redacts_every_leaf(
    caplog, transport_first
):
    caplog.set_level(logging.WARNING, logger="mcp_multiplexer")
    refusal = _mcp_error(-32603, "refused password=refusal-synthetic")
    transport = ConnectionRefusedError("lost password=transport-synthetic")
    leaves = [transport, refusal] if transport_first else [refusal, transport]
    exc = _group(_group(leaves[0]), leaves[1])

    result = mux_module._child_failure_result("s", "t", exc)

    text = gateway_ops._child_error_text(result)
    assert gateway_ops._child_error_code(result) == "UNAVAILABLE"
    for secret in ("refusal-synthetic", "transport-synthetic"):
        assert secret not in text
        assert secret not in caplog.text
    assert caplog.text.index(type(leaves[0]).__name__) < caplog.text.index(
        type(leaves[1]).__name__
    )


def test_nested_refusals_keep_first_leaf_message():
    exc = _group(
        ValueError("opaque"),
        _group(_mcp_error(-32603, "first refusal")),
        _mcp_error(-32603, "second refusal"),
    )

    result = mux_module._child_failure_result("s", "t", exc)

    assert gateway_ops._child_error_code(result) == "CHILD_REFUSED"
    assert "first refusal" in gateway_ops._child_error_text(result)
    assert "second refusal" not in gateway_ops._child_error_text(result)
    assert "opaque" not in gateway_ops._child_error_text(result)


def test_empty_unknown_leaf_logs_class_without_separator(caplog):
    caplog.set_level(logging.WARNING, logger="mcp_multiplexer")

    result = mux_module._child_failure_result("s", "t", ValueError())

    assert "causes=ValueError)" in caplog.text
    assert "ValueError:" not in caplog.text
    assert gateway_ops._child_error_code(result) == "CHILD_REFUSED"


async def test_caller_cancellation_is_not_classified(monkeypatch):
    import asyncio

    mux = mux_module.MCPMultiplexer.__new__(mux_module.MCPMultiplexer)
    mux._catalog_epoch = 0
    monkeypatch.setattr(mux_module, "_require_fleet_capability", lambda *a, **k: None)
    monkeypatch.setattr(type(mux), "load_catalog", lambda self: {"s": {}})

    async def cancelled(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(mux, "_call_ephemeral_child_session", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await mux._call_service_child_once("s", "t", {}, timeout=1.0)


def _frozen_nested_arguments():
    from arbora.api.invoke.steps import freeze_claims

    return freeze_claims(
        {
            "project_id": 7,
            "api_parameters": {"issue_type": "incident", "labels": ["a", "b"]},
        }
    )


@pytest.mark.asyncio
async def test_governed_call_hands_children_plain_json_for_nested_objects(
    monkeypatch,
):
    """A pipeline-frozen object argument must reach the child as plain JSON."""
    import contextlib

    seen = {}

    def delegated(caller, *, allow_admitted_anonymous=False):
        assert allow_admitted_anonymous is True
        return contextlib.nullcontext()

    monkeypatch.setattr(gateway_ops, "_verified_delegation", delegated)
    monkeypatch.setattr(gateway_ops, "commit_native_dispatch", lambda *a: None)

    class Mux:
        async def call_admitted_child_once(self, server, tool, arguments):
            mux_module._assert_bounded_delegated_value(arguments)
            seen["arguments"] = arguments
            return mcp.types.CallToolResult(content=[], isError=False)

    delegated = gateway_ops.oauth_delegated_call_for_mux(Mux())
    gateway = object.__new__(gateway_ops.FleetGateway)
    gateway._delegated_call = delegated
    gateway._service_call = None

    async def admitted(_caller, _server, _tool):
        from types import SimpleNamespace

        from arbora.api.registry import Effect

        return SimpleNamespace(
            annotations={},
            effect_override=Effect.WRITE,
            credential_mode="delegated",
        )

    gateway._admitted_tool = admitted
    caller = type("C", (), {"principal": "p", "tenant": "t", "request_id": "r"})()
    from arbora.api.registry import Effect

    await gateway.call(
        caller,
        "gitlab",
        "gitlab_update_issue",
        _frozen_nested_arguments(),
        expected_effect=Effect.WRITE,
    )
    assert seen["arguments"] == {
        "project_id": 7,
        "api_parameters": {"issue_type": "incident", "labels": ["a", "b"]},
    }
    assert type(seen["arguments"]["api_parameters"]) is dict
    assert type(seen["arguments"]["api_parameters"]["labels"]) is list


def test_delegated_bound_check_rejects_frozen_mapping():
    """The child-argument bound is for plain JSON; frozen arguments need thawing."""
    with pytest.raises(ToolError):
        mux_module._assert_bounded_delegated_value(_frozen_nested_arguments())


def test_indeterminate_message_states_unknown_outcome():
    from arbora.api.errors import to_envelope
    from arbora.api.invoke.steps import OpError

    _status, envelope = to_envelope(
        OpError("INDETERMINATE", {"audit_ref": ""}),
        op="fleet.call",
        request_id="r",
        registry_digest="d",
    )
    message = envelope["error"]["message"]
    assert message != "Request refused" and "unknown" in message.lower()


def test_unacquired_reservation_and_timeout_log_their_cause(caplog):
    source = inspect.getsource(pipeline)
    assert "effect not acquired" in source and "dispatch timed out" in source
