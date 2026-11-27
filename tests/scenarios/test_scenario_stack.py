"""The scenario stack serves HTTP and MCP over one engine (ARBORA-ACCEPTANCE-R006.5)."""

from __future__ import annotations

import pytest

from arbora.testing.scenario_stack import (
    SCENARIO_STACK_INCOMPLETE,
    ScenarioRefused,
    ScenarioStack,
)
from tests.scenarios.conftest import scenario_stack  # noqa: F401


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.5")
def test_stack_serves_mcp_and_http_over_one_engine(
    scenario_stack: ScenarioStack,  # noqa: F811
) -> None:
    stack = scenario_stack
    stack.require("application", "engine", "http", "mcp")

    stack.http(
        "memory.write",
        {"key": "claim-1", "value": {"text": "deploy window is closed"}},
        **{"Idempotency-Key": "k-1"},
    )
    over_http = stack.http("memory.read", {"key": "claim-1"})
    over_mcp = stack.mcp("ask", "memory.read", {"key": "claim-1"})

    assert over_http["value"] == {"text": "deploy window is closed"}
    assert "deploy window is closed" in str(over_mcp)
    # One engine saw the write and both reads: HTTP and MCP share the application.
    methods = [method for method, *_ in stack.engine.sent]
    assert methods.count("KvPut") == 1
    assert methods.count("KvGet") >= 2


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.5")
def test_a_stack_without_a_needed_component_fails_instead_of_skipping() -> None:
    bare = ScenarioStack(
        application=object(), engine=None, http=lambda *a: None, mcp=None
    )  # type: ignore[arg-type]
    with pytest.raises(ScenarioRefused) as refused:
        bare.require("application", "engine", "mcp", "connectors")
    assert refused.value.code == SCENARIO_STACK_INCOMPLETE
    assert refused.value.details == {"missing": ["connectors", "engine", "mcp"]}
