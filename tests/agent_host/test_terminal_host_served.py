"""Authenticated terminal log handoff through the served host app (ARBORA-HOST-R009.5, R009.6, R010.7).

The served host app runs for real over HTTP with ``HostLogAccess``; identity, grant
and record owners are the controlled owners of ``test_log_access``. The terminal
client side (TUIG-RUNTIME-R002) reads through exactly these routes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arbora.agent_host.log_access import HostLogAccess
from tests.agent_host.test_log_access import (
    ControlledOwners,
    read,
    reference,
    serve,
)


def _access(
    tmp_path: Path, owners: ControlledOwners, mode: Any, web_logs: bool
) -> HostLogAccess:
    return HostLogAccess(
        sessions=owners,
        grants=owners,
        source=owners,
        workspace=str(tmp_path),
        host_instance="host-1",
        interface_mode=mode,
        web_logs=web_logs,
        clock=lambda: owners.now,
    )


@pytest.mark.spec("ARBORA-HOST-R009.5")
def test_effective_log_preference_is_authenticated(tmp_path: Path) -> None:
    owners = ControlledOwners()
    access = _access(tmp_path, owners, "terminal", True)
    with serve(access) as client:
        response = client.post(
            "/host/logs/reference", headers={"Authorization": "Bearer a"}
        )
        binding = response.json()
        assert binding["web_logs"] is True and binding["interface_mode"] == "terminal"
        assert binding["host_instance"] == "host-1"
        assert binding["tenant"] == "tenant-a" and binding["principal"] == "alice"
        assert binding["session_ref"] == "session-alice"
        assert binding["source_generations"] == ["host-1"]
        assert binding["expires_at_ms"] == 190_000
        ref = binding["log_ref"]
        assert read(client, ref).status_code == 200
        served = owners.calls
        # Each binding changed alone yields no read and no record.
        for field, value in (
            ("tenant", "tenant-x"),
            ("principal", "mallory"),
            ("session_ref", "session-x"),
            ("workspace", "/elsewhere"),
            ("policy_revision", "policy-x"),
            ("source_generations", ("host-2",)),
            ("expires_at_ms", 100),
        ):
            owners.change = {field: value}
            denied = read(client, ref)
            assert denied.status_code == 403 and "records" not in denied.json()
        owners.change = {}
        owners.now = 201
        assert read(client, ref).status_code == 403
    assert owners.calls == served


@pytest.mark.spec("ARBORA-HOST-R009.6")
@pytest.mark.parametrize("mode", ["terminal", "web", "both"])
@pytest.mark.parametrize("preference", [True, False])
def test_terminal_log_mode_preference_matrix(
    tmp_path: Path, mode: Any, preference: bool
) -> None:
    owners = ControlledOwners()
    with serve(_access(tmp_path, owners, mode, preference)) as client:
        response = client.post(
            "/host/logs/reference", headers={"Authorization": "Bearer a"}
        )
        shown = mode in {"terminal", "both"} and preference
        if shown:
            assert response.status_code == 200
            assert read(client, response.json()["log_ref"]).status_code == 200
            assert owners.calls == 1
        else:
            assert response.status_code == 403
            assert owners.calls == 0
        # HTTP serving stays available in every mode.
        assert client.get("/openapi.json").status_code in {200, 404}


@pytest.mark.spec("ARBORA-HOST-R010.7")
def test_terminal_log_handoff_preserves_scope_and_lifecycle(tmp_path: Path) -> None:
    owners = ControlledOwners()
    with serve(_access(tmp_path, owners, "both", True)) as client:
        a, b = reference(client), reference(client, "b")
        first = read(client, a)
        assert first.json()["records"] == [{"message": "redacted tenant-a"}]
        assert read(client, b, "b").json()["records"] == [
            {"message": "redacted tenant-b"}
        ]
        # Reconnect: the same session resumes from its own cursor; another cannot.
        assert read(client, a, cursor=first.json()["cursor"]).status_code == 200
        assert read(client, a, "b").status_code == 403
        assert read(client, b, "a", cursor=first.json()["cursor"]).status_code == 403
        # Denial clears the handle: later reads reach no record source.
        owners.allowed = False
        calls = owners.calls
        assert read(client, a).status_code == 403
        owners.allowed = True
        owners.revision = "grant-2"
        assert read(client, a).status_code == 403
        assert owners.calls == calls
        # A fresh reference under the new grant reads again.
        assert read(client, reference(client)).status_code == 200
