"""Served city edit layer: versioned, actor-recorded, plan-confirmed (ARBORA-FLEET-R087)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke import OpError, OpResult, invoke
from arbora.api.invoke.pipeline import (
    OperationRefused,
    _declared_owners,
    uncovered_write_operations,
)
from arbora.api.ops import fleet_city
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Confirm, Effect, Surface
from tests.fleet._city_support import caller, services_with

LAYER = {"version": 3, "edits": [{"pin": "building:one", "cell": "1:1"}]}


class FakeEditPort:
    def __init__(self, refused: tuple[str, dict[str, Any]] | None = None) -> None:
        self.refused = refused
        self.puts: list[dict[str, Any]] = []

    async def get(self, *, tenant: str, context: Any) -> Any:
        return LAYER

    async def put(self, *, tenant: str, edits: Any, base_version: int, context: Any):
        self.puts.append({"tenant": tenant, "base_version": base_version})
        if self.refused:
            raise OperationRefused(*self.refused)
        return {"version": base_version + 1, "actor": "person:one"}


def _context(port: Any | None) -> Any:
    services = {} if port is None else {"city_edit_layer": port}
    return SimpleNamespace(
        services=services, caller=SimpleNamespace(tenant="tenant:one")
    )


def _op(op_id: str) -> Any:
    return next(o for o in fleet_city.operations() if o.id == op_id)


PARAMS = {"edits": [{"pin": "building:one", "cell": "2:2"}], "base_version": 3}


@pytest.mark.spec("ARBORA-FLEET-R087")
def test_edit_layer_round_trips_with_actor_and_refuses_collisions() -> None:
    port = FakeEditPort()
    got = asyncio.run(
        invoke(
            "fleet.city.edit_layer.get",
            {},
            caller(),
            Surface.HTTP,
            services=services_with({"city_edit_layer": port}),
        )
    )
    assert isinstance(got, OpResult) and got.value["value"] == LAYER

    put = asyncio.run(
        fleet_city.handle_edit_layer_put(
            _context(port), PARAMS, _op("fleet.city.edit_layer.put")
        )
    )
    assert put["value"] == {"version": 4, "actor": "person:one"}
    assert port.puts == [{"tenant": "tenant:one", "base_version": 3}]

    for code in ("PIN_COLLISION", "STALE_EDIT"):
        with pytest.raises(OperationRefused) as caught:
            asyncio.run(
                fleet_city.handle_edit_layer_put(
                    _context(FakeEditPort((code, {}))),
                    PARAMS,
                    _op("fleet.city.edit_layer.put"),
                )
            )
        assert caught.value.code == "REVISION_CONFLICT"
        assert caught.value.details == {"reason": code}


@pytest.mark.spec("ARBORA-FLEET-R087")
def test_edit_layer_unbound_refuses_and_put_is_plan_confirmed_with_replay_owner() -> (
    None
):
    unbound = asyncio.run(
        invoke(
            "fleet.city.edit_layer.get",
            {},
            caller(),
            Surface.HTTP,
            services=services_with({}),
        )
    )
    assert isinstance(unbound, OpError) and unbound.code == "UNAVAILABLE"
    with pytest.raises(OperationRefused) as caught:
        asyncio.run(
            fleet_city.handle_edit_layer_put(
                _context(None), PARAMS, _op("fleet.city.edit_layer.put")
            )
        )
    assert caught.value.code == "UNAVAILABLE"

    spec = _op("fleet.city.edit_layer.put")
    assert spec.effect is Effect.WRITE and spec.confirm is Confirm.PLAN
    assert spec.scopes == frozenset({"fleet:write"})
    assert (
        _declared_owners()["fleet.city.edit_layer.put"] == "medulla:WorldViewEditLayer"
    )
    assert "fleet.city.edit_layer.put" not in uncovered_write_operations(get_registry())
