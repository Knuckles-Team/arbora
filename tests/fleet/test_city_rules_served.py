"""Served city rules: registry, growth rules and seed from the engine (ARBORA-FLEET-R088)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arbora.api.invoke import OpError, OpResult, invoke
from arbora.api.registry import Effect, Surface
from tests.fleet._city_support import caller, services_with

RULES = {
    "registry": {"Agent": "tower"},
    "growth": [{"when": "tools>10", "then": "add_floor"}],
    "seed": 42,
}


class FakeRulesPort:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def rules(self, *, tenant: str, context: Any) -> Any:
        self.calls.append(tenant)
        return RULES


def _run(port: Any | None) -> Any:
    return asyncio.run(
        invoke(
            "fleet.city.rules",
            {},
            caller(),
            Surface.HTTP,
            services=services_with({} if port is None else {"city_rules": port}),
        )
    )


@pytest.mark.spec("ARBORA-FLEET-R088")
def test_rules_serve_registry_growth_and_seed_from_the_engine() -> None:
    port = FakeRulesPort()
    result = _run(port)
    assert isinstance(result, OpResult) and result.code == "OK", result
    assert result.value["value"] == RULES
    assert port.calls == ["tenant:one"]
    unbound = _run(None)
    assert isinstance(unbound, OpError) and unbound.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-FLEET-R088")
def test_rules_is_a_scoped_read_that_hard_codes_nothing() -> None:
    from arbora.api.ops import fleet_city

    spec = next(o for o in fleet_city.operations() if o.id == "fleet.city.rules")
    assert spec.effect is Effect.READ and spec.scopes == frozenset({"fleet:read"})
