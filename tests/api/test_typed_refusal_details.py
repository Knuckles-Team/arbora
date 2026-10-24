"""Typed refusals name their reason (ARBORA-OPS-R009.4, gaps G14 and G10).

A refusal's public envelope names the fields and the engine reason code, never
an input value or the engine's own detail text.
"""

from __future__ import annotations

import pytest
from test_invoke_contract import caller, run, setup_services

from arbora.api.errors import EngineRefusal, engine_reason_details, to_envelope
from arbora.api.ops import get_registry
from arbora.api.registry import Surface
from arbora.mcp_server.served_invocation import validate_provider_schema

pytestmark = pytest.mark.spec("ARBORA-OPS-R009.4")


async def _no_fleet_effect(op, params, supplied):
    raise AssertionError("no fleet call")


def _served():
    registry = get_registry()
    services, runtime, _, _, _ = setup_services(
        registry=registry,
        external_effect_operations=frozenset(
            op for op in ("fleet.call", "messaging.alert") if registry.get(op)
        ),
        fleet_effect=_no_fleet_effect,
        schema_validate=validate_provider_schema,
    )
    return services, runtime


def _envelope(result):
    _, envelope = to_envelope(
        result, op="query.uql", request_id="r-1", registry_digest="d" * 64
    )
    return envelope["error"]


def test_direct_uql_with_the_wrong_field_names_both_fields() -> None:
    """G14: ``{"query": ...}`` is refused naming ``query`` and ``text``."""
    services, runtime = _served()
    secret_text = "MATCH (n:Incident) |> LIMIT 3"
    result = run(
        services,
        supplied=caller(effective_scopes=frozenset({"query:unified"})),
        op_id="query.uql",
        params={"query": secret_text},
        surface=Surface.MCP,
    )
    assert result.code == "INVALID_ARGUMENT" and runtime.calls == []
    error = _envelope(result)
    assert error["details"]["unexpected"] == ["query"]
    assert error["details"]["missing"] == ["text"]
    assert secret_text not in str(error)


def test_a_typed_model_refusal_names_its_fields() -> None:
    services, _ = _served()
    result = run(
        services,
        supplied=caller(effective_scopes=frozenset({"ingest:read"})),
        op_id="ingest.jobs.status",
        params={"job": "x"},
        surface=Surface.MCP,
    )
    error = _envelope(result)
    assert error["code"] == "INVALID_ARGUMENT"
    assert "job_id" in error["details"]["fields"]


def test_an_engine_refusal_keeps_its_reason_code_not_its_text() -> None:
    detail = "NlQuery produced invalid UQL: UQL_TRAILING_TOKENS at 1:51, found `(` near 'db01'"
    details = engine_reason_details(detail)
    assert details == {"reason_code": "UQL_TRAILING_TOKENS", "at": "1:51"}
    from arbora.api.invoke.pipeline import _typed_refusal

    error = _envelope(
        _typed_refusal(EngineRefusal("INVALID_ARGUMENT", details=details))
    )
    assert error["source"] == "engine"
    assert error["details"] == details
    assert "db01" not in str(error)


def test_unreviewed_detail_keys_never_reach_the_envelope() -> None:
    from arbora.api.invoke.steps import OpError

    error = _envelope(
        OpError("INVALID_ARGUMENT", {"field": "x", "value": "secret", "reason": "r"})
    )
    assert error["details"] == {"field": "x", "reason": "r"}
