"""The host publishes the ranking schemas once per tenant (ARBORA-FLEET-R083.5).

The engine does not publish the ``decide.schema.catalog.*`` components itself
(``MEDULLA-DECISION-ENGINE-R829.4``). Arbora publishes the engine's exported body at
start-up, as it does the task-plan schema, then pins each digest the engine returns.
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.domain.fleet.ranking import QUESTION_IDS, HostRanking
from arbora.api.task_plan_schema import catalog_rank_pins, publish_at_startup, rank_pin
from arbora.mcp_server.host_context import bind_ranking
from tests.fleet.test_ranking_served import (
    PRINCIPAL,
    TENANT,
    LoopbackEngine,
    _find,
)

BODY = {
    "schema_version": 1,
    "features": [
        {
            "name": "name_overlap",
            "kind": {"feature": "number", "key": "rank.name_overlap"},
            "missing": {"missing": "impute", "value": {"scale": "q32", "value": 0}},
        }
    ],
}


class Stat:
    """The engine body codec: the digest is the hash of the compact body."""

    @staticmethod
    def encode_body(body: Any) -> tuple[str, Any]:
        import hashlib
        import json

        digest = hashlib.sha256(
            json.dumps(body, separators=(",", ":")).encode()
        ).hexdigest()
        return "sha256:" + digest, {}


def _export(corrupt: bool = False) -> dict[str, dict[str, Any]]:
    digest, _ = Stat.encode_body(BODY)
    return {
        q: {
            "component_id": f"decide.schema.{q}",
            "kind": "feature_schema",
            "schema_version": 1,
            "content_digest": "sha256:bad" if corrupt else digest,
            "idempotency_key": f"eg:{q}",
            "body": BODY,
        }
        for q in sorted(QUESTION_IDS)
    }


@pytest.fixture
def engine_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = types.ModuleType("fake_engine_exports")
    monkeypatch.setitem(sys.modules, "fake_engine_exports", module)
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "9.9.9")
    return module


@pytest.mark.spec("ARBORA-FLEET-R083.5")
def test_publish_once_per_tenant_then_find_ranks_through_the_engine(
    engine_module: Any,
) -> None:
    engine_module.CATALOG_RANK_FEATURE_SCHEMAS = _export()
    pins = catalog_rank_pins(modules=("fake_engine_exports",), stat=Stat())
    assert sorted(pins) == sorted(QUESTION_IDS)
    assert rank_pin(pins, TENANT, "catalog.operation_rank") is None  # unpublished

    sent: list[tuple[str, str]] = []

    async def publish(tenant: str, pin: Any) -> str:
        sent.append((tenant, pin["component_id"]))
        return "sha256:" + "d" * 64

    for pin in pins.values():
        done = asyncio.run(publish_at_startup(pin, publish, (TENANT, TENANT)))
        assert done == {TENANT: True}
    assert len(sent) == len(QUESTION_IDS)  # once per tenant per question
    dependency = rank_pin(pins, TENANT, "catalog.operation_rank")
    assert dependency is not None
    assert dependency["component_id"] == "decide.schema.catalog.operation_rank"

    engine = LoopbackEngine(order=lambda ids: list(reversed(ids)))
    host = SimpleNamespace(fleet=SimpleNamespace(ranking=HostRanking()))
    bind_ranking(
        host,
        SimpleNamespace(
            engine_send=engine.send,
            decision_feature_schema=lambda _t: {"component_id": "decide.schema.other"},
            decision_principal=lambda: PRINCIPAL,
            decision_tenant=lambda: TENANT,
            ranking_feature_schema=lambda t, q: rank_pin(pins, t, q),
        ),
    )
    assert host.fleet.ranking.status().source == "medulla"
    assert _find(host.fleet.ranking)["result"]["ranking"] == "medulla"


@pytest.mark.spec("ARBORA-FLEET-R083.5")
def test_an_engine_without_the_export_publishes_nothing(engine_module: Any) -> None:
    assert catalog_rank_pins(modules=("fake_engine_exports",), stat=Stat()) == {}


@pytest.mark.spec("ARBORA-FLEET-R083.5")
def test_an_export_that_does_not_match_its_digest_is_refused(
    engine_module: Any,
) -> None:
    engine_module.CATALOG_RANK_FEATURE_SCHEMAS = _export(corrupt=True)
    assert catalog_rank_pins(modules=("fake_engine_exports",), stat=Stat()) == {}
