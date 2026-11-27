"""Shared served-invoke composition for the city world-view operations."""

from __future__ import annotations

import contextlib
from typing import Any

from arbora.api.invoke import invoke
from arbora.api.invoke.executor import BoundOperationRuntime
from arbora.api.mcp.resolve import IntentResolver
from arbora.api.ops import fleet_city as fleet_city_ops
from arbora.api.policy.eunomia import PolicyGate
from arbora.api.registry import Registry
from arbora.mcp_server.serving import MCPProjection
from arbora.observability.state import SubsystemWindows
from tests.api._support import make_verified_caller
from tests.api.test_invoke_contract import setup_services

caller = make_verified_caller("fleet:read")


class _ClaimsClient:
    """One shared fake client that records the bound per-request claims."""

    def __init__(self) -> None:
        self.claims: dict[str, Any] | None = None

    @contextlib.contextmanager
    def use_verified_context(self, claims: dict[str, Any]):
        self.claims = claims
        try:
            yield self
        finally:
            self.claims = None


def services_with(bindings: dict[str, Any]) -> Any:
    """One invoke composition whose composite dispatch reaches the real handler."""
    client = _ClaimsClient()

    async def caller_client(_tenant: str) -> Any:
        return client

    async def verify_current(current: Any) -> Any:
        return current

    async def service_client(_tenant: str) -> Any:
        return client

    async def service_claims(_tenant: str, required_scopes: frozenset[str]) -> dict:
        return {
            "principal": "service:arbora",
            "tenant": _tenant,
            "scopes": sorted(required_scopes),
            "policy_version": "v1",
            "delegation": False,
        }

    async def check_access(_client: Any, _current: Any, _subject: str) -> bool:
        return False

    async def eg_dispatch(_binding: Any, _params: Any, _context: Any) -> Any:
        raise AssertionError("city ops never dispatch the engine port")

    runtime = BoundOperationRuntime(
        caller_client=caller_client,
        verify_current=verify_current,
        service_client=service_client,
        service_claims=service_claims,
        check_access=check_access,
        eg_dispatch=eg_dispatch,
        service_scopes=frozenset({"example:execute"}),
        service_principal="service:arbora",
        bindings=bindings,
    )
    services, *_rest = setup_services(
        effects=None,
        registry=Registry(fleet_city_ops.operations()),
        runtime=runtime,
    )
    return services


def projection(services: Any) -> MCPProjection:
    return MCPProjection(
        services.registry,
        services,
        IntentResolver(),
        lambda: caller(),
        PolicyGate("none"),
        invoke,
        windows=SubsystemWindows(),
    )
