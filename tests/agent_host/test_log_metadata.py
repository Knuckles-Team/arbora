"""Versioned HTTP projection of real retained host records."""
# spec: ARBORA-HOST-R009.8.2.2

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from arbora.agent_host.log_access import HostLogBatch, HostLogRecord
from arbora.agent_host.log_sink import WorkspaceLogSink
from tests.agent_host.test_log_access import ControlledOwners, reference, serve
from tests.agent_host.test_log_replay import access_for


def test_version_two_preserves_restart_metadata_and_reports_gap(tmp_path: Path) -> None:
    owners = ControlledOwners()
    sink = WorkspaceLogSink(str(tmp_path), "host-1", max_bytes=1024)
    asyncio.run(
        sink.emit(tenant="tenant-a", request_id="original-request", message="one")
    )
    sink.close()
    sink = WorkspaceLogSink(str(tmp_path), "host-2", max_bytes=1024)
    try:
        with serve(access_for(sink, owners)) as client:
            ref = reference(client)
            body = {"version": 2, "log_ref": ref, "cursor": None, "limit": 128}
            response = client.post(
                "/host/logs/read", headers={"Authorization": "Bearer a"}, json=body
            )
            assert response.status_code == 200
            payload = response.json()
            assert set(payload) == {"version", "records", "cursor", "gap"}
            assert payload["version"] == 2
            record = payload["records"][0]
            assert record == {
                "host_instance": "host-1",
                "sequence": 1,
                "request": "original-request",
                "timestamp": record["timestamp"],
                "severity": "INFO",
                "message": "one",
            }
            assert record["timestamp"] > 0
            for index in range(30):
                asyncio.run(
                    sink.emit(
                        tenant="tenant-a", request_id=str(index), message="x" * 400
                    )
                )
            body["cursor"] = payload["cursor"]
            response = client.post(
                "/host/logs/read", headers={"Authorization": "Bearer a"}, json=body
            )
            assert response.status_code == 409
            assert response.json() == {"detail": "retention_gap"}
            body["version"] = 3
            assert (
                client.post(
                    "/host/logs/read", headers={"Authorization": "Bearer a"}, json=body
                ).status_code
                == 422
            )
    finally:
        sink.close()


@pytest.mark.parametrize(
    "change",
    [
        {"timestamp": 10**400},
        {"sequence": 0},
        {"request": "x" * 1025},
        {"severity": ""},
    ],
)
def test_invalid_owner_metadata_refuses_without_server_error(
    tmp_path: Path, change: dict
) -> None:
    owners = ControlledOwners()
    owners.batch = HostLogBatch(
        (
            replace(
                HostLogRecord(
                    "tenant-a",
                    str(tmp_path),
                    "host-1",
                    "private",
                    sequence=1,
                    timestamp=1,
                ),
                **change,
            ),
        ),
        None,
    )
    from arbora.agent_host.log_access import HostLogAccess

    access = HostLogAccess(
        sessions=owners,
        grants=owners,
        source=owners,
        workspace=str(tmp_path),
        host_instance="host-1",
        interface_mode="terminal",
        web_logs=True,
        clock=lambda: owners.now,
    )
    with serve(access) as client:
        response = client.post(
            "/host/logs/read",
            headers={"Authorization": "Bearer a"},
            json={"version": 2, "log_ref": reference(client)},
        )
        assert response.status_code == 503
        assert response.json() == {"detail": "log_unavailable"}


@pytest.mark.spec("ARBORA-HOST-R009.8.2.2")
def test_message_only_batch_is_refused_with_log_schema_unsupported(
    tmp_path: Path,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from arbora.agent_host.log_routes import register_host_log_routes

    owners = ControlledOwners()
    sink = WorkspaceLogSink(str(tmp_path), "host-1", max_bytes=1024)
    asyncio.run(sink.emit(tenant="tenant-a", request_id="r1", message="one"))
    app = FastAPI()
    app.state.public_base_url = "http://127.0.0.1:1"
    register_host_log_routes(app, access_for(sink, owners), metadata_required=True)
    try:
        client = TestClient(app, base_url="http://127.0.0.1:1")
        ref = reference(client)
        headers = {"Authorization": "Bearer a"}
        refused = client.post(
            "/host/logs/read", headers=headers, json={"version": 1, "log_ref": ref}
        )
        assert refused.status_code == 400
        assert refused.json() == {"detail": "LOG_SCHEMA_UNSUPPORTED"}
        served = client.post(
            "/host/logs/read", headers=headers, json={"version": 2, "log_ref": ref}
        )
        assert served.status_code == 200
        assert served.json()["records"][0]["request"] == "r1"
    finally:
        sink.close()
