"""Served host-log contract the Overstory log view reads, over real HTTP and real retention files.

The browser leg is the Overstory host-log view test, which drives the same sequence in a
real browser (consumer row OVERSTORY-APPS-R054).
"""
# spec: ARBORA-HOST-R009.8.2.2.2

import asyncio
from pathlib import Path

import pytest

from arbora.agent_host.log_sink import WorkspaceLogSink
from tests.agent_host.test_log_access import ControlledOwners, reference, serve
from tests.agent_host.test_log_replay import access_for

RECORD_FIELDS = {
    "host_instance",
    "sequence",
    "request",
    "timestamp",
    "severity",
    "message",
}


def read_v2(client, ref: str, cursor: str | None = None):
    return client.post(
        "/host/logs/read",
        headers={"Authorization": "Bearer a"},
        json={"version": 2, "log_ref": ref, "cursor": cursor, "limit": 128},
    )


@pytest.mark.spec("ARBORA-HOST-R009.8.2.2.2")
def test_retention_gap_clears_protected_content(tmp_path: Path) -> None:
    owners = ControlledOwners()
    sink = WorkspaceLogSink(str(tmp_path), "host-1", max_bytes=1024)
    asyncio.run(
        sink.emit(tenant="tenant-a", request_id="origin-request", message="held-text")
    )
    try:
        with serve(access_for(sink, owners)) as client:
            ref = reference(client)
            first = read_v2(client, ref)
            assert first.status_code == 200
            batch = first.json()
            assert batch["gap"] is False
            (record,) = batch["records"]
            assert set(record) == RECORD_FIELDS
            assert (record["host_instance"], record["request"]) == (
                "host-1",
                "origin-request",
            )
            assert record["sequence"] == 1 and record["message"] == "held-text"

            for index in range(30):
                asyncio.run(
                    sink.emit(
                        tenant="tenant-a", request_id=str(index), message="x" * 400
                    )
                )
            # A stale cursor answers one fixed refusal and carries no retained text,
            # metadata or cursor: the web view drops what it holds on this answer.
            stale = read_v2(client, ref, batch["cursor"])
            assert stale.status_code == 409
            assert stale.json() == {"detail": "retention_gap"}
            assert "held-text" not in stale.text

            # A fresh read past the rotation reports the gap, keeps every record's
            # original host metadata and never replays the rotated-out content.
            resumed = read_v2(client, reference(client))
            assert resumed.status_code == 200
            tail = resumed.json()
            assert tail["gap"] is True
            assert tail["records"] and tail["cursor"]
            assert all(set(item) == RECORD_FIELDS for item in tail["records"])
            assert all(item["host_instance"] == "host-1" for item in tail["records"])
            assert min(item["sequence"] for item in tail["records"]) > 1
            assert "held-text" not in resumed.text

            # Revocation clears the same way: one fixed refusal and no content.
            owners.allowed = False
            denied = read_v2(client, ref, tail["cursor"])
            assert denied.status_code == 403
            assert denied.json() == {"detail": "workspace_denied"}
    finally:
        sink.close()


def browser_headers(client, token: str, *, csrf: bool = True, origin: bool = True):
    from arbora.identity.browser import SESSION_COOKIE, csrf_token_for

    headers = {"Cookie": f"{SESSION_COOKIE}={token}"}
    if origin:
        headers["Origin"] = str(client.base_url).rstrip("/")
    if csrf:
        headers["X-CSRF-Token"] = csrf_token_for(token)
    return headers


@pytest.mark.spec("ARBORA-HOST-R009.8.2.2.2")
def test_browser_session_reads_logs_with_csrf_and_loses_them_on_revocation(
    tmp_path: Path,
) -> None:
    import secrets

    owners = ControlledOwners()
    token = secrets.token_urlsafe(32)
    owners.states[token] = owners.states["a"]
    sink = WorkspaceLogSink(str(tmp_path), "host-1", max_bytes=1024)
    asyncio.run(sink.emit(tenant="tenant-a", request_id="r1", message="held-text"))
    try:
        with serve(access_for(sink, owners)) as client:
            good = browser_headers(client, token)

            # A session cookie with its CSRF proof and exact origin reads the log.
            issued = client.post("/host/logs/reference", headers=good)
            assert issued.status_code == 200, issued.text
            ref = issued.json()["log_ref"]
            read = client.post(
                "/host/logs/read",
                headers=good,
                json={"version": 2, "log_ref": ref, "cursor": None, "limit": 128},
            )
            assert read.status_code == 200
            assert [r["message"] for r in read.json()["records"]] == ["held-text"]

            # A cookie without the CSRF proof, or without the exact origin, is
            # refused on both routes and carries no log content.
            for headers in (
                browser_headers(client, token, csrf=False),
                browser_headers(client, token, origin=False),
                {**good, "Origin": "http://evil.example"},
                {**good, "X-CSRF-Token": "x" * 43},
            ):
                for path, body in (
                    ("/host/logs/reference", None),
                    (
                        "/host/logs/read",
                        {"version": 2, "log_ref": ref, "cursor": None, "limit": 128},
                    ),
                ):
                    refused = client.post(path, headers=headers, json=body)
                    assert refused.status_code == 403
                    assert refused.json() == {"detail": "workspace_denied"}

            # A cookie together with a bearer is ambiguous and refused; the bearer
            # alone keeps working for non-browser callers.
            both = client.post(
                "/host/logs/reference", headers={**good, "Authorization": "Bearer a"}
            )
            assert both.status_code == 403
            assert reference(client)

            # A revoked grant is refused for the browser session as for a bearer.
            owners.allowed = False
            denied = client.post(
                "/host/logs/read",
                headers=good,
                json={"version": 2, "log_ref": ref, "cursor": None, "limit": 128},
            )
            assert denied.status_code == 403
            assert denied.json() == {"detail": "workspace_denied"}
            assert "held-text" not in denied.text
    finally:
        sink.close()
