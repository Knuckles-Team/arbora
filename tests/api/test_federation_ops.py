"""Federation source operations (ARBORA-OPS-R019.1, R019.2) and engine passthrough."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import federation, query_engine
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Effect, EgMethod
from tests.api._ops_support import service_context


class _Catalog:
    def __init__(self) -> None:
        self.records = {
            "s1": {
                "source_id": "s1",
                "owner": "alice",
                "grantees": [],
                "config": {
                    "host": "db",
                    "password": "hunter2",
                    "nested": {"api_key": "k"},
                },
            }
        }
        self.probes: list[str] = []

    async def get(self, source_id: str, *, tenant: str) -> dict[str, Any] | None:
        return self.records.get(source_id)

    async def list(self, *, tenant: str) -> list[dict[str, Any]]:
        return list(self.records.values())

    async def probe(self, source_id: str, *, tenant: str) -> dict[str, Any]:
        self.probes.append(source_id)
        return {"ok": True, "token": "t0ps3cret", "latency_ms": 3}

    async def grant(self, source_id: str, grantee: str, *, tenant: str, **_: Any):
        self.records[source_id]["grantees"].append(grantee)
        return self.records[source_id]

    async def revoke(self, source_id: str, grantee: str, *, tenant: str, **_: Any):
        self.records[source_id]["grantees"].remove(grantee)
        return self.records[source_id]


def _ctx(catalog: _Catalog | None, who: str):
    services = {} if catalog is None else {"federation_sources": catalog}
    return service_context(tenant="t", principal=who, **services)


def _op(op_id: str):
    return next(o for o in federation.operations() if o.id == op_id)


async def _call(op_id: str, catalog: _Catalog | None, who: str, **params: Any):
    return await federation.handle_federation(_ctx(catalog, who), params, _op(op_id))


def test_register_is_a_governed_engine_write_and_reads_pass_through() -> None:
    registry = get_registry()
    register = registry["federation.sources.register"]
    assert register.binding.handler == "arbora.api.ops.federation.handle_federation"
    assert register.effect is Effect.ADMIN and register.scopes == {"federation:admin"}
    for op in query_engine.operations():
        assert isinstance(op.binding, EgMethod)
        assert registry[op.id] is op or registry[op.id].binding == op.binding
    for op_id in (
        "query.sparql",
        "query.sql",
        "ontology.inspect",
        "ontology.schema.classes",
        "ontology.validate",
    ):
        assert registry[op_id].effect is Effect.READ


@pytest.mark.asyncio
async def test_register_governed_and_probe_redacts_credentials() -> None:
    register = get_registry()["federation.sources.register"]
    assert register.effect is Effect.ADMIN
    assert register.idempotency.value == "key_required"
    assert register.audit.value == "event"
    catalog = _Catalog()
    probe = await _call("federation.sources.probe", catalog, "alice", source_id="s1")
    assert probe["value"]["token"] == federation.REDACTED
    assert probe["value"]["ok"] is True
    got = await _call("federation.sources.get", catalog, "alice", source_id="s1")
    assert got["value"]["config"]["password"] == federation.REDACTED
    assert got["value"]["config"]["nested"]["api_key"] == federation.REDACTED
    assert got["value"]["config"]["host"] == "db"
    with pytest.raises(OperationRefused):
        await _call("federation.sources.list", None, "alice")


@pytest.mark.asyncio
async def test_only_owner_and_grantees_see_source_and_revoke_blocks_probe() -> None:
    catalog = _Catalog()
    listed = lambda who: _call("federation.sources.list", catalog, who)  # noqa: E731
    assert len((await listed("alice"))["value"]["sources"]) == 1
    assert (await listed("bob"))["value"]["sources"] == []
    with pytest.raises(LookupError):
        await _call("federation.sources.probe", catalog, "bob", source_id="s1")
    # A non-grantee cannot even see the source, so cannot share it.
    with pytest.raises(LookupError):
        await _call(
            "federation.sources.share", catalog, "bob", source_id="s1", grantee="bob"
        )
    await _call(
        "federation.sources.share", catalog, "alice", source_id="s1", grantee="bob"
    )
    assert len((await listed("bob"))["value"]["sources"]) == 1
    await _call("federation.sources.probe", catalog, "bob", source_id="s1")
    assert catalog.probes == ["s1"]
    # Grantees cannot unshare.
    with pytest.raises(PermissionError):
        await _call(
            "federation.sources.unshare", catalog, "bob", source_id="s1", grantee="bob"
        )
    await _call(
        "federation.sources.unshare", catalog, "alice", source_id="s1", grantee="bob"
    )
    with pytest.raises(LookupError):
        await _call("federation.sources.probe", catalog, "bob", source_id="s1")
    assert catalog.probes == ["s1"]
