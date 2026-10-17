"""Controlled owner fixtures prove served composition, not deployed grants."""
# spec: GRAPHOS-HOST-R009.7

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from graph_os.agent_host import build_agent_host_app
from graph_os.agent_host.log_access import (
    HostLogAccess,
    HostLogBatch,
    HostLogRecord,
    WorkspaceLogGrant,
)
from graph_os.identity.engine import Resolution
from graph_os.identity.ports import SessionState


def state(tenant: str, principal: str, session: str) -> SessionState:
    context = {
        "tenant": tenant,
        "principal": principal,
        "agent_id": principal,
        "audience": "host",
        "roles": (),
        "scopes": (),
        "delegation": (),
        "policy_version": "policy-1",
    }
    resolution = Resolution(
        principal_id=principal,
        username=principal,
        kind="human",
        status="active",
        is_bootstrap=False,
        roles=(),
        groups=(),
        scopes=(),
        mfa_required=False,
        mfa_enrolled=False,
        session_mfa_pending=False,
        request_context=context,
    )
    return SessionState(resolution, 200_000, session, None)


class ControlledOwners:
    """Explicit test identity/grant/source owners; no production issuer."""

    def __init__(self) -> None:
        self.states = {
            "a": state("tenant-a", "alice", "session-alice"),
            "b": state("tenant-b", "bob", "session-bob"),
            "a2": state("tenant-a", "alice", "session-alice-other"),
        }
        self.allowed = True
        self.revision = "grant-1"
        self.change: dict[str, Any] = {}
        self.calls = 0
        self.on_read: Callable[[], None] = lambda: None
        self.on_grant: Callable[[], None] = lambda: None
        self.batch: HostLogBatch | None = None
        self.now = 100.0
        self.slow = False

    async def resolve_session(self, credential: str) -> SessionState:
        if credential not in self.states:
            raise PermissionError("private credential failure")
        return self.states[credential]

    async def authorize(
        self, session: SessionState, *, workspace: str
    ) -> WorkspaceLogGrant | None:
        self.on_grant()
        if not self.allowed:
            return None
        return replace(
            WorkspaceLogGrant(
                tenant=str(session.resolution.request_context["tenant"]),
                principal=session.resolution.principal_id,
                session_ref=session.session_ref,
                workspace=workspace,
                policy_revision="policy-1",
                revision=self.revision,
                expires_at_ms=190_000,
                action="host.logs.read",
            ),
            **self.change,
        )

    async def read(
        self,
        *,
        tenant: str,
        workspace: str,
        host_instance: str,
        cursor: str | None,
        limit: int,
    ) -> HostLogBatch:
        self.calls += 1
        self.on_read()
        if self.slow:
            await asyncio.sleep(5)
        return self.batch or HostLogBatch(
            (HostLogRecord(tenant, workspace, host_instance, f"redacted {tenant}"),),
            f"private-cursor-{tenant}",
        )


@contextmanager
def serve(access: HostLogAccess | None) -> Iterator[httpx.Client]:
    app = build_agent_host_app(graph=None, graph_config={}, host_logs=access)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="critical", lifespan="off")
        )
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        thread.start()
        deadline = time.monotonic() + 3
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            assert server.started
            with httpx.Client(
                base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", timeout=3
            ) as client:
                yield client
        finally:
            server.should_exit = True
            thread.join(5)
            assert not thread.is_alive()


@pytest.fixture
def setup(tmp_path: Path) -> tuple[ControlledOwners, HostLogAccess]:
    owners = ControlledOwners()
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
    return owners, access


def reference(client: httpx.Client, credential: str = "a") -> str:
    response = client.post(
        "/host/logs/reference", headers={"Authorization": f"Bearer {credential}"}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["log_ref"])


def read(
    client: httpx.Client, ref: str, credential: str = "a", **body: Any
) -> httpx.Response:
    return client.post(
        "/host/logs/read",
        headers={"Authorization": f"Bearer {credential}"},
        json={"log_ref": ref, **body},
    )


def test_success_isolation_reconnect_and_foreign_cursor(
    setup: tuple[ControlledOwners, HostLogAccess],
) -> None:
    owners, access = setup
    with serve(access) as client:
        a, b = reference(client), reference(client, "b")
        first = read(client, a)
        assert first.json()["records"] == [{"message": "redacted tenant-a"}]
        assert read(client, b, "b").json()["records"] == [
            {"message": "redacted tenant-b"}
        ]
        assert read(client, a, cursor=first.json()["cursor"]).status_code == 200
        count = owners.calls
        for credential in ("b", "a2", "missing"):
            assert read(client, a, credential).status_code == 403
        assert read(client, a, cursor="private-cursor-tenant-b").status_code == 403
        assert read(client, "forged").status_code == 403
        assert owners.calls == count


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant", "foreign"),
        ("principal", "foreign"),
        ("session_ref", "foreign"),
        ("workspace", "/foreign"),
        ("policy_revision", "new"),
        ("action", "host.start"),
        ("expires_at_ms", 90_000),
        ("expires_at_ms", 300_000),
        ("revision", ""),
    ],
)
def test_grant_mismatches_never_read(
    setup: tuple[ControlledOwners, HostLogAccess], field: str, value: Any
) -> None:
    owners, access = setup
    with serve(access) as client:
        ref = reference(client)
        owners.change[field] = value
        assert read(client, ref).status_code == 403
        assert owners.calls == 0


@pytest.mark.parametrize("mutation", ["denied", "revision", "expired", "session"])
def test_revocation_during_read_discards_all_bytes(
    setup: tuple[ControlledOwners, HostLogAccess], mutation: str
) -> None:
    owners, access = setup

    def revoke() -> None:
        if mutation == "denied":
            owners.allowed = False
        elif mutation == "revision":
            owners.revision = "grant-2"
        elif mutation == "expired":
            owners.now = 201
        else:
            del owners.states["a"]

    owners.on_read = revoke
    with serve(access) as client:
        response = read(client, reference(client))
        assert response.status_code == 403
        assert "redacted" not in response.text
        assert owners.calls == 1


def test_session_revoked_while_grant_awaits(
    setup: tuple[ControlledOwners, HostLogAccess],
) -> None:
    owners, access = setup

    def revoke() -> None:
        owners.states.pop("a", None)

    owners.on_grant = revoke
    with serve(access) as client:
        assert (
            client.post(
                "/host/logs/reference", headers={"Authorization": "Bearer a"}
            ).status_code
            == 403
        )
        assert owners.calls == 0


@pytest.mark.parametrize("bad", ["foreign", "oversized", "count"])
def test_source_bounds_before_serialization(
    setup: tuple[ControlledOwners, HostLogAccess], tmp_path: Path, bad: str
) -> None:
    owners, access = setup
    record = HostLogRecord("tenant-a", str(tmp_path), "host-1", "safe")
    records: tuple[HostLogRecord, ...] = (record,)
    if bad == "foreign":
        records = (replace(record, tenant="tenant-b", message="private secret"),)
    elif bad == "oversized":
        records = (replace(record, message="x" * 16385),)
    else:
        records = (record,) * 129
    owners.batch = HostLogBatch(records, None)
    with serve(access) as client:
        response = read(client, reference(client))
        assert response.status_code == 403
        assert "private secret" not in response.text


def test_default_and_denied_grant_zero_source_reads(
    setup: tuple[ControlledOwners, HostLogAccess],
) -> None:
    owners, access = setup
    with serve(None) as client:
        assert client.post("/host/logs/reference").status_code == 503
        assert read(client, "unavailable").status_code == 503
        assert client.get("/health").status_code == 200
    owners.allowed = False
    with serve(access) as client:
        assert (
            client.post(
                "/host/logs/reference", headers={"Authorization": "Bearer a"}
            ).status_code
            == 403
        )
    assert owners.calls == 0


def test_ambiguous_credentials_and_authority_params_refused(
    setup: tuple[ControlledOwners, HostLogAccess],
) -> None:
    owners, access = setup
    with serve(access) as client:
        ref = reference(client)
        headers = [("Authorization", "Bearer a"), ("Authorization", "Bearer b")]
        assert client.post("/host/logs/reference", headers=headers).status_code == 403
        assert (
            client.post(
                "/host/logs/reference",
                headers={"Authorization": "Bearer a", "Cookie": "session=x"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/host/logs/reference?credential=a",
                headers={"Authorization": "Bearer a"},
            ).status_code
            == 403
        )
        assert read(client, ref, tenant="tenant-b").status_code == 422
        assert read(client, ref, limit=True).status_code == 422
        assert owners.calls == 0


def test_read_timeout(setup: tuple[ControlledOwners, HostLogAccess]) -> None:
    owners, access = setup
    owners.slow = True
    with serve(access) as client:
        response = read(client, reference(client))
        assert response.status_code == 503


@pytest.mark.parametrize(
    "mode,enabled", [("terminal", False), ("both", False), ("web", True)]
)
def test_preference_or_mode_denies_before_owners(
    tmp_path: Path, mode: Any, enabled: bool
) -> None:
    owners = ControlledOwners()
    access = HostLogAccess(
        sessions=owners,
        grants=owners,
        source=owners,
        workspace=str(tmp_path),
        host_instance="host-1",
        interface_mode=mode,
        web_logs=enabled,
        clock=lambda: owners.now,
    )
    with serve(access) as client:
        assert (
            client.post(
                "/host/logs/reference", headers={"Authorization": "Bearer a"}
            ).status_code
            == 403
        )
    assert owners.calls == 0


def test_reference_limits_and_expiry_cleanup(
    setup: tuple[ControlledOwners, HostLogAccess],
) -> None:
    owners, access = setup

    async def fill() -> None:
        for _ in range(1024):
            await access.reference("a")
        with pytest.raises(PermissionError):
            await access.reference("a")
        owners.now = 191
        owners.change["expires_at_ms"] = 195_000
        assert await access.reference("a")

    asyncio.run(fill())


def test_concurrent_read_and_cancellation_release(
    setup: tuple[ControlledOwners, HostLogAccess],
) -> None:
    owners, access = setup

    async def exercise() -> None:
        ref = await access.reference("a")
        started = asyncio.Event()
        owners.slow = True
        owners.on_read = started.set
        pending = asyncio.create_task(access.read("a", ref, cursor=None, limit=128))
        await started.wait()
        with pytest.raises(PermissionError):
            await access.read("a", ref, cursor=None, limit=128)
        assert owners.calls == 1
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        owners.slow = False
        batch = await access.read("a", ref, cursor=None, limit=128)
        assert owners.calls == 2
        with pytest.raises(PermissionError):
            await access.read("a", ref, cursor=None, limit=128)
        assert owners.calls == 2
        assert await access.read("a", ref, cursor=batch.cursor, limit=128)

    asyncio.run(exercise())
