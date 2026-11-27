"""The host composes the engine world-view port behind the city operations (ARBORA-FLEET-R086 to R088).

The bindings come from the host's own composition function, so each served call reaches
the engine through the one port. Two shapes are exercised: the pinned release, which
refuses ``UNAVAILABLE`` naming the method, and a structural double of the engine's
``main`` shape, which answers the envelope unchanged.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke import OpError, OpResult, invoke
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import fleet_city
from arbora.api.registry import Surface
from arbora.mcp_server.served_invocation import _with_city_ports
from tests.fleet._city_support import caller, services_with

READS = (
    ("fleet.city.layout", {}, "WorldViewLayout"),
    ("fleet.city.chunk", {"anchor": "n:1", "cursor": "c1"}, "WorldViewChunk"),
    ("fleet.city.edit_layer.get", {}, "WorldViewEditLayerGet"),
    ("fleet.city.rules", {}, "WorldViewRules"),
)
PUT = {"edits": [{"pin": "building:one", "cell": "2:2"}], "base_version": 3}


def _bound() -> dict[str, Any]:
    bindings = _with_city_ports({})
    assert bindings is not None
    return dict(bindings)


def _call(op_id: str, params: dict[str, Any]) -> Any:
    return asyncio.run(
        invoke(
            op_id,
            params,
            caller(),
            Surface.HTTP,
            services=services_with(_bound()),
        )
    )


def test_the_host_binds_one_port_to_the_four_city_services() -> None:
    from arbora.fleet.city_ports import CITY_SERVICES, EngineWorldViewPort

    bindings = _bound()
    assert set(bindings) == set(CITY_SERVICES)
    assert {type(port) for port in bindings.values()} == {EngineWorldViewPort}
    assert _with_city_ports(None) is None
    own = object()
    assert _with_city_ports({"city_rules": own})["city_rules"] is own  # type: ignore[index]


@pytest.mark.spec("ARBORA-FLEET-R086")
@pytest.mark.spec("ARBORA-FLEET-R087")
@pytest.mark.spec("ARBORA-FLEET-R088")
@pytest.mark.parametrize(("op_id", "params", "method"), READS)
def test_each_city_read_refuses_unavailable_naming_the_method_on_the_pinned_release(
    op_id: str, params: dict[str, Any], method: str
) -> None:
    refused = _call(op_id, params)
    assert isinstance(refused, OpError) and refused.code == "UNAVAILABLE"
    assert method in str(refused.details)


@pytest.mark.spec("ARBORA-FLEET-R087")
def test_the_edit_layer_write_refuses_unavailable_naming_the_method_on_the_pinned_release() -> (
    None
):
    context = SimpleNamespace(
        services=_bound(), caller=SimpleNamespace(tenant="tenant:one")
    )
    spec = next(
        o for o in fleet_city.operations() if o.id == "fleet.city.edit_layer.put"
    )
    with pytest.raises(OperationRefused) as caught:
        asyncio.run(fleet_city.handle_edit_layer_put(context, PUT, spec))
    assert caught.value.code == "UNAVAILABLE"
    assert "WorldViewEditLayerPut" in str(caught.value.details)


class _MainEngine:
    """A structural double of the engine's ``main`` world-view methods."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], bool]] = []
        self.refuse: str | None = None

    async def __call__(
        self,
        context: Any,
        method: str,
        params: dict[str, Any],
        *,
        idempotent: bool = False,
    ) -> Any:
        self.sent.append((method, params, idempotent))
        if self.refuse and method == "WorldViewEditLayerPut":
            raise OperationRefused(self.refuse, {})
        return {"method": method, "tenant": params["tenant_id"], "version": 4}


@pytest.fixture
def main_engine(monkeypatch: pytest.MonkeyPatch) -> _MainEngine:
    engine = _MainEngine()
    monkeypatch.setattr("arbora.pin_lag.medulla.require_methods", lambda *m: None)
    monkeypatch.setattr("arbora.pin_lag.medulla.engine_send", engine)
    return engine


@pytest.mark.spec("ARBORA-FLEET-R086")
@pytest.mark.spec("ARBORA-FLEET-R087")
@pytest.mark.spec("ARBORA-FLEET-R088")
@pytest.mark.parametrize(("op_id", "params", "method"), READS)
def test_each_city_read_serves_the_engine_answer_unchanged_on_the_main_shape(
    main_engine: _MainEngine, op_id: str, params: dict[str, Any], method: str
) -> None:
    result = _call(op_id, params)
    assert isinstance(result, OpResult) and result.code == "OK", result
    assert result.value["value"] == {
        "method": method,
        "tenant": "tenant:one",
        "version": 4,
    }
    sent_method, sent_params, idempotent = main_engine.sent[-1]
    assert (sent_method, sent_params["tenant_id"], idempotent) == (
        method,
        "tenant:one",
        False,
    )
    if method == "WorldViewChunk":
        assert (sent_params["anchor"], sent_params["cursor"]) == ("n:1", "c1")


@pytest.mark.spec("ARBORA-FLEET-R087")
def test_the_edit_layer_write_reaches_the_engine_idempotently_and_maps_its_refusals(
    main_engine: _MainEngine,
) -> None:
    context = SimpleNamespace(
        services=_bound(), caller=SimpleNamespace(tenant="tenant:one")
    )
    spec = next(
        o for o in fleet_city.operations() if o.id == "fleet.city.edit_layer.put"
    )
    put = asyncio.run(fleet_city.handle_edit_layer_put(context, PUT, spec))
    assert put["value"]["version"] == 4
    method, params, idempotent = main_engine.sent[-1]
    assert (method, params["base_version"], idempotent) == (
        "WorldViewEditLayerPut",
        3,
        True,
    )
    for code in ("PIN_COLLISION", "STALE_EDIT"):
        main_engine.refuse = code
        with pytest.raises(OperationRefused) as caught:
            asyncio.run(fleet_city.handle_edit_layer_put(context, PUT, spec))
        assert caught.value.code == "REVISION_CONFLICT"
