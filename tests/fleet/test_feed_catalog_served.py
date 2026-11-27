"""ARBORA-FLEET-R008.2: feed events reach the catalog sanitized and tenant-scoped."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.fleet.catalog_composition import compose_multiplexer_ops
from arbora.fleet.feeds import FEED_INTAKE, FeedEventRejected, FeedIntake


class _Reader:
    async def read(self) -> Any:
        return SimpleNamespace(servers=())


class _Mux:
    _probe_cache: dict[str, Mapping[str, Any]] = {}

    def status_snapshot(self) -> Mapping[str, Any]:
        return {"children": {}}


async def _yes(_item: Any, _caller: Any) -> bool:
    return True


async def _noop(*_args: Any) -> Any:
    return True


def _ops(intake: FeedIntake) -> Any:
    return compose_multiplexer_ops(
        reader=_Reader(),
        mux=_Mux(),
        sdk_entries=intake.entries,
        visible=_yes,
        loadable=_yes,
        callable_item=_yes,
        mount=_noop,
        notify=_noop,
        invoke=_noop,
        session_key_for=lambda _caller: None,
    )


def _caller(tenant: str) -> Any:
    return SimpleNamespace(effective_scopes=frozenset({"mcp:discover"}), tenant=tenant)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R008.2")
async def test_feed_events_sanitized_and_tenant_scoped() -> None:
    intake = FeedIntake()
    for kind in ("rum", "security_audit", "cicd"):
        intake.ingest(kind, tenant="acme", source="feed", payload={"k": kind})
    intake.ingest("rum", tenant="other", source="feed", payload={"k": "rum"})
    with pytest.raises(FeedEventRejected):
        intake.ingest("rum", tenant="acme", source="feed", payload={"token": "x"})
    with pytest.raises(FeedEventRejected):
        intake.ingest("bogus", tenant="acme", source="feed", payload={})

    ops = _ops(intake)
    found = await ops.find_tools(_caller("acme"), browse=True)
    ids = [item["id"] for item in found["items"]]
    assert len(ids) == 3
    assert {i.split("/")[0] for i in ids} == {
        "connector:feed-rum",
        "connector:feed-security_audit",
        "connector:feed-cicd",
    }
    assert "token" not in str(found)
    theirs = await ops.find_tools(_caller("other"), browse=True)
    assert len(theirs["items"]) == 1
    nobody = await ops.find_tools(_caller("third"), browse=True)
    assert nobody["items"] == []


@pytest.mark.spec("ARBORA-FLEET-R008.2")
def test_host_binds_the_intake_into_the_served_catalog() -> None:
    import inspect

    from arbora.mcp_server import fleet_composition

    assert "sdk_entries=FEED_INTAKE.entries" in inspect.getsource(
        fleet_composition.bind_fleet_gateway
    )
    assert isinstance(FEED_INTAKE, FeedIntake)
