"""SCIM routes and the directory sync job are composed by the served host.

ARBORA-IDENTITY-R011.2.4 and ARBORA-IDENTITY-R010.2.3.2: landed means reachable, so the
tests compose the served invocation through the real composition function and reach SCIM
over the mounted API application and the sync job through the host's background services.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from starlette.testclient import TestClient

from arbora.access.action_policy import ActionPolicies
from arbora.core.settings import runtime_config
from arbora.identity.directory_sync import JOB_ID
from arbora.identity.host_services import (
    SCIM_CREDENTIALS_PATH,
    DirectorySyncService,
    scim_ports_from_store,
)
from arbora.identity.ldap import DirectoryUser
from arbora.mcp_server.served_invocation import (
    compose_served_invocation,
    refused_fleet_effect,
)
from arbora.security import request_identity
from arbora.security.actor_context import ActorContext, ActorType
from tests.test_served_operation_api import FakeClient, FakePool

USER = {
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
    "userName": "ada",
    "active": True,
}


class _Admin:
    """The engine administration port double: records every ``Identity`` request."""

    def __init__(self, existing: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.existing = existing or []

    async def identity(self, client, claims, family, op, request=None, *, expect):
        self.calls.append((family, op, request))
        assert "identity:admin" in claims["scopes"]
        if (family, op) == ("user", "list"):
            return self.existing
        if (family, op) == ("user", "create"):
            return {"principal_id": "p-new"}
        return {}


class _Store:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def get(self, name: str) -> str | None:
        assert name == SCIM_CREDENTIALS_PATH
        return self.value


class _Directory:
    def __init__(self) -> None:
        self.binds = 0

    def bind(self, config: Any) -> None:
        self.binds += 1

    def snapshot(self) -> list[DirectoryUser]:
        return [
            DirectoryUser("ann", ("cn=eng",)),
            DirectoryUser("eve", ("cn=eng",), disabled=True),
        ]


class _Clock:
    def now(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        return None


def _compose(monkeypatch: pytest.MonkeyPatch, admin: _Admin, **changes: Any) -> Any:
    monkeypatch.setattr(runtime_config, "auth_jwt_audience", "arbora-fixture")
    monkeypatch.setattr(runtime_config, "kg_policy_version", "policy:fixture")
    actor = ActorContext(
        actor_id="person:one",
        actor_type=ActorType.HUMAN,
        tenant_id="tenant:one",
        authenticated=True,
        roles=("kg:read", "identity:admin"),
        credential_expires_at=int(time.time()) + 3600,
    )
    process = request_identity.mint_graph_session(actor)
    return compose_served_invocation(
        pool=FakePool(FakeClient()),
        graph=process.graph,
        process_session=lambda: process,
        action_policies=ActionPolicies(),
        bindings={"identity_admin": admin},
        fleet_effect=refused_fleet_effect,
        **changes,
    )


@pytest.mark.spec("ARBORA-IDENTITY-R011.2.4")
def test_served_api_mounts_scim_over_the_service_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = _Admin()
    store = _Store(json.dumps({"okta": "tok-okta"}))
    invocation = _compose(monkeypatch, admin, scim_store=store)
    client = TestClient(invocation.http_application())
    auth = {"Authorization": "Bearer tok-okta"}
    created = client.post("/scim/v2/okta/Users", json=USER, headers=auth)
    assert created.status_code == 201 and created.json() == {"id": "p-new"}
    off = {**USER, "active": False}
    assert (
        client.patch("/scim/v2/okta/Users/p-new", json=off, headers=auth).status_code
        == 200
    )
    assert client.delete("/scim/v2/okta/Users/p-new", headers=auth).status_code == 204
    assert ("user", "create", {"username": "ada", "roles": [], "kind": "human"}) in (
        admin.calls
    )
    assert admin.calls.count(("session", "revoke_all", {"id": "p-new"})) == 2
    assert (
        client.post("/scim/v2/other/Users", json=USER, headers=auth).status_code == 403
    )
    assert client.post("/scim/v2/okta/Users", json=USER).status_code == 401


@pytest.mark.spec("ARBORA-IDENTITY-R011.2.4")
def test_no_delivered_credential_mounts_no_scim_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = _compose(monkeypatch, _Admin(), scim_store=_Store(None))
    assert invocation.scim_ports is None
    client = TestClient(invocation.http_application())
    assert client.post("/scim/v2/okta/Users", json=USER).status_code == 404
    assert scim_ports_from_store(_Store("not json"), object()) is None
    assert scim_ports_from_store(_Store("{}"), object()) is None


@pytest.mark.spec("ARBORA-IDENTITY-R010.2.3.2")
def test_host_schedules_the_directory_sync_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = {
        "ARBORA_LDAP_HOST": "ldap.test",
        "ARBORA_LDAP_BIND_DN": "cn=svc",
        "ARBORA_LDAP_GROUP_ROLES": json.dumps({"cn=eng": ["engineer"]}),
        "ARBORA_LDAP_MAX_DEPROVISION_FRACTION": "0.6",
        "ARBORA_CURATOR_TICK_S": "0",
    }
    import arbora.core.settings as core_settings

    real = core_settings.setting
    monkeypatch.setattr(
        core_settings,
        "setting",
        lambda name, default=None, **kw: table.get(name, real(name, default, **kw)),
    )
    admin = _Admin(existing=[{"username": "eve", "principal_id": "p-eve", "roles": []}])
    directory = _Directory()
    invocation = _compose(
        monkeypatch,
        admin,
        scim_store=_Store(None),
        directory_factory=lambda: directory,
        scheduler_clock=_Clock(),
    )
    jobs = [
        s for s in invocation.background_services if isinstance(s, DirectorySyncService)
    ]
    assert [job.job_id for job in jobs] == [JOB_ID]

    async def scenario() -> None:
        assert await jobs[0].tick() is True
        assert directory.binds == 1
        runs = {"n": 0}

        def should_stop() -> bool:
            runs["n"] += 1
            return runs["n"] > 1

        await jobs[0].run(should_stop)

    asyncio.run(scenario())
    kinds = [(family, op) for family, op, _ in admin.calls]
    assert ("user", "create") in kinds and ("user", "set_status") in kinds
    assert ("session", "revoke_all") in kinds
    assert directory.binds == 2


@pytest.mark.spec("ARBORA-IDENTITY-R010.2.3.2")
def test_no_ldap_settings_schedule_no_job(monkeypatch: pytest.MonkeyPatch) -> None:
    invocation = _compose(monkeypatch, _Admin(), scim_store=_Store(None))
    assert not [
        s for s in invocation.background_services if isinstance(s, DirectorySyncService)
    ]
