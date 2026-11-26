"""Convergence status is a served read (ARBORA-FLEET-R051.2.1)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.fleet import (
    ConvergenceResult,
    handle_catalog_convergence,
    operations,
)
from arbora.api.registry import Surface
from arbora.fleet.catalog_reader import CatalogGeneration, GenerationStatus

DIGEST = "a" * 64


class Gateway:
    def __init__(self, status: GenerationStatus) -> None:
        self.status = status
        self.calls = 0

    async def generation_status(self, caller: Any) -> GenerationStatus:
        self.calls += 1
        return self.status


def context(gateway: Gateway | None, tenant: str = "tenant-a") -> SimpleNamespace:
    return SimpleNamespace(
        caller=SimpleNamespace(tenant=tenant),
        services={} if gateway is None else {"fleet_gateway": gateway},
    )


def status(tenant: str = "tenant-a", state: str = "converged") -> GenerationStatus:
    return GenerationStatus(
        replica="a",
        state=state,  # type: ignore[arg-type]
        reason="catalog_generation_behind" if state == "degraded" else None,
        active=CatalogGeneration(tenant, 7, DIGEST),
        engine_generation=7,
    )


@pytest.mark.spec("ARBORA-FLEET-R051.2.1")
async def test_convergence_read_over_mcp() -> None:
    op = {o.id: o for o in operations()}["fleet.catalog.convergence"]
    assert op.verb.value == "ask" and op.effect.value == "read"
    assert op.scopes == frozenset({"agent:pack-control"})
    assert Surface.MCP in op.surfaces
    assert op.result is ConvergenceResult

    gateway = Gateway(status(state="degraded"))
    result = await handle_catalog_convergence(context(gateway), {}, op)
    parsed = ConvergenceResult.model_validate(result)
    row = parsed.replicas[0]
    assert row.state == "degraded" and row.reason == "catalog_generation_behind"
    assert row.generation == {"tenant": "tenant-a", "generation": 7, "digest": DIGEST}
    assert row.engine_generation == 7
    assert gateway.calls == 1


@pytest.mark.spec("ARBORA-FLEET-R051.2.1")
async def test_a_generation_of_another_tenant_is_denied() -> None:
    with pytest.raises(OperationRefused) as refused:
        await handle_catalog_convergence(
            context(Gateway(status(tenant="tenant-b"))),
            {},
            None,  # type: ignore[arg-type]
        )
    assert refused.value.code == "TENANT_DENIED"


@pytest.mark.spec("ARBORA-FLEET-R051.2.1")
async def test_an_unbound_gateway_is_not_papered_over() -> None:
    with pytest.raises(RuntimeError):
        await handle_catalog_convergence(context(None), {}, None)  # type: ignore[arg-type]
