"""The mounted SCIM Users routes (ARBORA-IDENTITY-R011.2.4)."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from arbora.identity.scim import SCIM_USER_SCHEMA, ScimServiceCredential
from arbora.identity.scim_routes import ScimPorts, scim_app

USER = {"schemas": [SCIM_USER_SCHEMA], "userName": "ada", "active": True}


class _Identity:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def create_user(self, user) -> str:
        self.calls.append(("create", user.user_name))
        return "u-1"

    def update_user(self, user_id, user) -> None:
        self.calls.append(("update", user_id, user.active))

    def deactivate_user(self, user_id) -> None:
        self.calls.append(("deactivate", user_id))


@pytest.mark.spec("ARBORA-IDENTITY-R011.2.4")
def test_create_update_patch_deactivate_and_foreign_token() -> None:
    identity, events = _Identity(), []
    ports = ScimPorts(
        credentials={"tok-a": ScimServiceCredential("okta", "t1")},
        identity=identity,
        emit=events.append,
    )
    client = TestClient(scim_app(ports))
    auth = {"Authorization": "Bearer tok-a"}
    assert client.post("/okta/Users", json=USER, headers=auth).status_code == 201
    assert client.put("/okta/Users/u-1", json=USER, headers=auth).status_code == 200
    off = {**USER, "active": False}
    assert client.patch("/okta/Users/u-1", json=off, headers=auth).status_code == 200
    assert client.delete("/okta/Users/u-1", headers=auth).status_code == 204
    assert [c[0] for c in identity.calls] == [
        "create",
        "update",
        "update",
        "deactivate",
    ]
    # A token for another provider answers 403; no or an unknown token answers 401.
    foreign = client.post("/other/Users", json=USER, headers=auth)
    assert foreign.status_code == 403
    assert foreign.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert client.post("/okta/Users", json=USER).status_code == 401
    assert client.post("/okta/Users", json={}, headers=auth).status_code == 400
    assert len(identity.calls) == 4  # the refusals wrote nothing
    assert all("tok-a" not in repr(event) for event in events)
    assert any(event.get("outcome") == "SCIM_PROVIDER_MISMATCH" for event in events)
