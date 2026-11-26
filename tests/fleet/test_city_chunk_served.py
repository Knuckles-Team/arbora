"""Served city chunk read: the engine envelope, unchanged (ARBORA-FLEET-R086)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arbora.api.invoke import OpError, OpResult, invoke
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.mcp.verbs import dispatch_verb
from arbora.api.registry import Effect, Surface
from tests.fleet._city_support import caller, projection, services_with

CHUNK = {
    "elements": (
        {"id": "building:one", "cell": "0:0", "provenance": {"source": "engine"}},
    ),
    "next_cursor": "opaque-2",
}


class FakeChunkPort:
    def __init__(self, refused: tuple[str, dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.refused = refused

    async def chunk(self, **kwargs: Any) -> Any:
        self.calls.append({k: v for k, v in kwargs.items() if k != "context"})
        if self.refused:
            raise OperationRefused(*self.refused)
        return CHUNK


def _run(params: dict[str, Any], port: Any | None) -> Any:
    bindings = {} if port is None else {"city_chunk": port}
    return asyncio.run(
        invoke(
            "fleet.city.chunk",
            params,
            caller(),
            Surface.HTTP,
            services=services_with(bindings),
        )
    )


@pytest.mark.spec("ARBORA-FLEET-R086")
def test_chunk_read_serves_the_engine_envelope_and_refuses_unbound() -> None:
    port = FakeChunkPort()
    result = _run({"cell_range": "0:0-4:4", "cursor": "opaque-1"}, port)
    assert isinstance(result, OpResult) and result.code == "OK", result
    assert result.value["value"] == CHUNK
    assert port.calls == [
        {
            "tenant": "tenant:one",
            "cell_range": "0:0-4:4",
            "anchor": None,
            "cursor": "opaque-1",
        }
    ]
    services = services_with({"city_chunk": port})
    mcp = asyncio.run(
        dispatch_verb(
            "ask", projection(services), op="fleet.city.chunk", params={"anchor": "n:1"}
        )
    )
    assert mcp["ok"] is True and mcp["result"]["value"] == CHUNK

    unbound = _run({"anchor": "n:1"}, None)
    assert isinstance(unbound, OpError) and unbound.code == "UNAVAILABLE"
    assert unbound.details == {"reason": "world view engine unbound"}

    for refusal in ("SUBJECT_ACCESS_DENIED", "NOT_FOUND"):
        refused = _run({"anchor": "n:1"}, FakeChunkPort((refusal, {})))
        assert isinstance(refused, OpError) and refused.code == refusal

    neither = _run({}, FakeChunkPort())
    assert isinstance(neither, OpError) and neither.code == "INVALID_ARGUMENT"


@pytest.mark.spec("ARBORA-FLEET-R086")
def test_chunk_is_a_scoped_read() -> None:
    from arbora.api.ops import fleet_city

    spec = next(o for o in fleet_city.operations() if o.id == "fleet.city.chunk")
    assert spec.effect is Effect.READ and spec.scopes == frozenset({"fleet:read"})
