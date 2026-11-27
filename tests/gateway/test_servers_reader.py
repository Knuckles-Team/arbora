"""ARBORA-FLEET-R033.1/.2: the typed ``servers`` reader behind the registry route."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arbora.gateway import registry_api
from arbora.gateway.servers_reader import (
    FleetCatalogServersReader,
    ServersCaller,
    ServersPageRequest,
    ServersRefused,
)
from arbora.security.session import ScopeError

ROWS = [
    {
        "id": "s1",
        "tenant_id": "acme",
        "name": "alpha",
        "transport": "http",
        "url": "https://alpha.example/mcp?token=abc",
        "enabled": True,
    },
    {
        "id": "s2",
        "tenant_id": "acme",
        "name": "beta",
        "transport": "stdio",
        "url": "https://beta.example/mcp/opaque-segment",
        "enabled": False,
    },
    {
        "id": "s3",
        "tenant_id": "other",
        "name": "gamma",
        "transport": "http",
        "url": "",
        "enabled": True,
    },
]


def _sql(statement: str) -> Any:
    tenant = re.search(r"tenant_id = '([^']*)'", statement)
    if "COUNT(*)" in statement:
        return [{"row_count": 0}]
    rows = [r for r in ROWS if tenant and r["tenant_id"] == tenant.group(1)]
    return sorted(rows, key=lambda r: r["name"])


def _session(tenant: str, principal: str, scopes: frozenset[str]) -> Any:
    actor = SimpleNamespace(
        tenant_id=tenant, actor_id=principal, authenticated=bool(principal)
    )

    def require_scope(scope: str) -> None:
        if scope not in scopes:
            raise ScopeError(scope)

    return SimpleNamespace(actor=actor, tenant=tenant, require_scope=require_scope)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    state = {"session": _session("acme", "alice", frozenset({"kg:read"}))}
    import arbora.security.session as session_module

    monkeypatch.setattr(registry_api, "get_catalog_engine", lambda _request: object())
    monkeypatch.setattr(registry_api, "_require_sql_exec", lambda _e: _sql)
    monkeypatch.setattr(session_module, "resolve_session", lambda: state["session"])
    app = FastAPI()
    registry_api.register_registry_routes(app)
    test_client = TestClient(app)
    test_client.state = state  # type: ignore[attr-defined]
    return test_client


@pytest.mark.spec("ARBORA-FLEET-R033.1")
def test_servers_scoped_by_tenant_and_principal(client: Any) -> None:
    body = client.get("/api/registry/servers").json()
    assert [i["name"] for i in body["items"]] == ["alpha", "beta"]
    assert body["items"][0]["url"] == ""
    assert body["items"][1]["url"] == "https://beta.example"
    assert "abc" not in str(body) and "opaque-segment" not in str(body)
    client.state["session"] = _session("other", "bob", frozenset({"kg:read"}))
    body = client.get("/api/registry/servers").json()
    assert [i["name"] for i in body["items"]] == ["gamma"]


@pytest.mark.spec("ARBORA-FLEET-R033.2")
def test_refusals_leak_no_records(client: Any) -> None:
    client.state["session"] = _session("acme", "", frozenset({"kg:read"}))
    r = client.get("/api/registry/servers")
    assert (r.status_code, r.json()) == (
        401,
        {"status": "refused", "code": "PRINCIPAL_REQUIRED"},
    )
    client.state["session"] = _session("acme", "alice", frozenset({"kg:read"}))
    r = client.get("/api/registry/servers?tenant=other")
    assert (r.status_code, r.json()) == (
        403,
        {"status": "refused", "code": "TENANT_MISMATCH"},
    )
    client.state["session"] = _session("acme", "alice", frozenset())
    r = client.get("/api/registry/servers")
    assert (r.status_code, r.json()) == (
        403,
        {"status": "refused", "code": "SCOPE_DENIED"},
    )


@pytest.mark.spec("ARBORA-FLEET-R033.1")
@pytest.mark.asyncio
async def test_reader_refuses_before_any_read() -> None:
    reader = FleetCatalogServersReader(object())
    with pytest.raises(ServersRefused) as refused:
        await reader.list(
            ServersCaller("acme", "alice", frozenset()), ServersPageRequest(limit=5)
        )
    assert refused.value.code == "SCOPE_DENIED"
