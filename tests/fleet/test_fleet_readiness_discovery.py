"""Fleet discovery, fleet calls and onboarding while the fleet comes up.

ARBORA-FLEET-R062: bounded fleet search, fast typed refusal, tolerant boot.
ARBORA-FLEET-R063: ``find(op="catalog")`` reaches the one catalog handler.
ARBORA-FLEET-R064: an onboarding refusal names its step and engine code.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.mcp.discovery import find_visible
from arbora.api.policy import PolicyGate
from arbora.fleet.declarations import Declarations
from arbora.mcp_server.fleet_composition import LateFleetGateway


@dataclass
class _Caller:
    principal: str = "person"
    tenant: str = "tenant-one"
    principal_kind: str = "human"
    delegated: bool = False
    authenticated: bool = True
    session: Any = None
    effective_scopes: frozenset[str] = frozenset({"mcp:discover", "read"})


class _Registry:
    digest = "digest-one"

    def __iter__(self):
        return iter(())

    def get(self, op_id: str) -> Any:
        return None


class _Resolver:
    def rank(self, verb, intent, items, *, scope_ref=None, top_k=20):
        return [
            SimpleNamespace(descriptor=SimpleNamespace(id=item["op"])) for item in items
        ]


def _find(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(
        find_visible(
            registry=_Registry(),
            caller=_Caller(),
            policy_gate=PolicyGate("none"),
            resolver=_Resolver(),
            **kwargs,
        )
    )


@pytest.mark.spec("ARBORA-FLEET-R062")
def test_find_returns_partial_when_fleet_search_times_out(monkeypatch) -> None:
    from arbora.core.settings import runtime_config

    monkeypatch.setattr(runtime_config, "mcp_dynamic_discovery_timeout", 0.05)

    async def slow(**_kwargs: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(5)
        return []

    found = _find(fleet_search=slow, intent="webarchiver")
    assert found["items"] == []
    assert found["fleet_partial"] is True
    assert found["fleet"] == "timeout"


@pytest.mark.spec("ARBORA-FLEET-R062")
def test_find_flags_not_ready_gateway() -> None:
    late = LateFleetGateway()
    found = _find(fleet_search=late.fleet_search, intent="webarchiver")
    assert found["fleet_partial"] is True
    assert found["fleet"] == "not_ready"


@pytest.mark.spec("ARBORA-FLEET-R062")
def test_fleet_call_refuses_fast_with_retry_hint_when_not_ready() -> None:
    from arbora.api.invoke.pipeline import OpError, _effective_op

    late = LateFleetGateway()
    services = SimpleNamespace(fleet_effect=late.effect)
    op = SimpleNamespace(id="fleet.call")
    caller: Any = _Caller()
    wired: Any = services
    outcome = asyncio.run(_effective_op(op, {}, caller, wired))
    assert isinstance(outcome, OpError)
    assert outcome.code == "UNAVAILABLE"
    assert outcome.details["reason"] == "fleet gateway is not ready"
    assert outcome.details["retry_after_s"] > 0


@pytest.mark.spec("ARBORA-FLEET-R062")
def test_engine_ready_wait_uses_configured_budget(monkeypatch) -> None:
    from arbora.core.settings import runtime_config
    from arbora.mcp_server import bootstrap

    assert bootstrap._engine_ready_timeout_seconds() >= 120.0
    monkeypatch.setattr(runtime_config, "arbora_engine_ready_timeout", 42.0)
    assert bootstrap._engine_ready_timeout_seconds() == 42.0


@pytest.mark.spec("ARBORA-FLEET-R063")
def test_find_catalog_op_serves_list_catalog() -> None:
    from arbora.fleet.gateway_ops import FleetGateway

    seen: list[tuple[str, bool]] = []

    class _Mux:
        async def list_catalog(self, server: str = "", include_tools: bool = True):
            seen.append((server, include_tools))
            return {"server": server, "tools": [{"tool": "t", "input_schema": {}}]}

    gateway = FleetGateway.__new__(FleetGateway)
    gateway._surface_mux = _Mux()
    late = LateFleetGateway()
    late.bind(gateway, ops=None)
    found = _find(
        fleet_search=late.fleet_search,
        fleet_catalog=late.fleet_catalog,
        op="catalog",
        params={"server": "gitlab-mcp"},
    )
    assert found["catalog"]["server"] == "gitlab-mcp"
    assert found["catalog"]["tools"][0]["tool"] == "t"
    assert seen == [("gitlab-mcp", True)]

    with pytest.raises(PermissionError):
        asyncio.run(gateway.catalog(_Caller(effective_scopes=frozenset()), server="x"))


@pytest.mark.spec("ARBORA-FLEET-R063")
def test_find_catalog_op_refuses_not_ready() -> None:
    from arbora.api.invoke.pipeline import OperationRefused

    late = LateFleetGateway()
    with pytest.raises(OperationRefused) as refused:
        _find(fleet_catalog=late.fleet_catalog, fleet_search=None, op="catalog")
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["retry_after_s"] > 0


@pytest.mark.spec("ARBORA-FLEET-R063")
def test_catalog_detail_rows_carry_input_schema() -> None:
    from arbora.fleet.multiplexer import MCPMultiplexer

    mux: Any = SimpleNamespace(
        _tool_enabled=lambda server_name, tool_name: True,
        tool_dispatchable=lambda prefixed_name: True,
    )
    schema = {"type": "object", "properties": {"url": {"type": "string"}}}
    rows = MCPMultiplexer._catalog_detail_tools(
        mux,
        "webarchiver-mcp",
        "webarchiver",
        {"tools": [{"name": "archive", "description": "d", "inputSchema": schema}]},
    )
    assert rows[0]["input_schema"] == schema


@pytest.mark.spec("ARBORA-FLEET-R064")
def test_onboarding_failure_names_step_and_engine_code(caplog) -> None:
    from arbora.fleet.onboarding import (
        FleetEndpoint,
        FleetOnboarding,
        OnboardingReport,
        _onboard_all,
    )

    class EngineResponseError(RuntimeError):
        def __init__(self, code: str, detail: str | None = None) -> None:
            self.code = code
            self.detail = detail
            super().__init__(f"{code}: {detail}" if detail else code)

    async def capture(endpoint: Any, *, auth: Any) -> Any:
        return SimpleNamespace(archive=SimpleNamespace(server=None, entries=()))

    async def refused(self: Any, endpoint: Any, pack: Any) -> bool:
        raise EngineResponseError("INTERNAL", "unclassified engine refusal")

    onboarding = FleetOnboarding(
        engine=None, session=None, capture=capture, declarations=Declarations()
    )
    endpoint = FleetEndpoint(name="webarchiver-mcp", url="http://example.test/mcp")
    report = OnboardingReport()

    import arbora.fleet.onboarding as module

    original = FleetOnboarding.pack_unchanged
    FleetOnboarding.pack_unchanged = refused  # type: ignore[method-assign]
    module_surface = module.pack_surface_report
    module.pack_surface_report = lambda pack: {}
    module_remember = module._remember_declaration
    module._remember_declaration = lambda name, pack: None  # type: ignore[assignment]
    module_auth = module._child_auth
    module._child_auth = lambda: None
    try:
        with caplog.at_level("WARNING"):
            asyncio.run(_onboard_all(onboarding, [endpoint], report))
    finally:
        FleetOnboarding.pack_unchanged = original  # type: ignore[method-assign]
        module.pack_surface_report = module_surface
        module._remember_declaration = module_remember
        module._child_auth = module_auth

    failure = report.failed["webarchiver-mcp"]
    assert failure.startswith(
        "OnboardingStepRefused: ConnectorPackStatus refused INTERNAL"
    )
    assert "engine-side classification gap" in failure
    assert "ConnectorPackStatus refused INTERNAL" in caplog.text
