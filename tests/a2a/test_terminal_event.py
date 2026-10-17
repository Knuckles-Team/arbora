"""Committed terminal point-read consumer boundaries (injected producer)."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.a2a.application import _get_task
from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.a2a.routing import OrchestratorA2ARouter
from arbora.a2a.service import A2AService
from arbora.a2a.terminal_event import (
    A2ATerminalEventUnavailable,
    project_terminal_event,
)

TASK = "a2a-" + "a" * 64
OWNER = "owner"
SESSION = SimpleNamespace(tenant="tenant", graph="graph")


def record() -> dict[str, Any]:
    return {
        "work_item": {
            "work_item_id": "workitem:orchestrator:" + TASK,
            "kind": "orchestrator_task",
            "status": "succeeded",
            "metadata": {
                "a2a_owner_ref": OWNER,
                "a2a_schema": "arbora-a2a-unary-v1",
            },
        },
        "terminal_event": {
            "status": "available",
            "tenant": "tenant",
            "scope": {
                "tenant": "physical-owner",
                "scope": {"kind": "graph", "graph": "graph"},
                "incarnation_id": "generation-1",
                "identity_digest": [11] * 32,
            },
            "batch_id": "batch-1",
            "commit_sequence": 7,
            "ordinal": 0,
            "run_id": TASK,
            "kind": "outcome",
            "outcome": "succeeded",
            "result_ref": None,
            "payload_digest": "c" * 64,
        },
    }


def project(value: Any) -> dict[str, Any]:
    return project_terminal_event(value, session=SESSION, task_id=TASK, owner_ref=OWNER)


@pytest.mark.spec("GRAPHOS-A2A-R010.1")
def test_committed_identity_is_projected_without_inventing_cursor() -> None:
    value = record()
    assert project(value) == value["terminal_event"]
    assert "cursor" not in project(value)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("tenant",), "foreign"),
        (("run_id",), "foreign"),
        (("outcome",), "failed"),
        (("kind",), "step"),
        (("commit_sequence",), 0),
        (("commit_sequence",), True),
        (("ordinal",), -1),
        (("payload_digest",), "not-a-digest"),
        (("scope", "identity_digest"), [0] * 31),
        (("scope", "identity_digest"), [True] * 32),
        (("scope", "identity_digest"), [256] * 32),
        (("batch_id",), "escape\x1b[31m"),
        (("result_ref",), "x" * 4097),
        (("cursor",), "invented"),
    ],
)
def test_invalid_or_foreign_event_never_projects(
    path: tuple[str, ...], value: Any
) -> None:
    raw = record()
    target = raw["terminal_event"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(A2ATerminalEventUnavailable):
        project(raw)


@pytest.mark.parametrize("key", ["work_item_id", "kind", "metadata"])
def test_foreign_work_item_refuses(key: str) -> None:
    raw = record()
    raw["work_item"][key] = "foreign"
    with pytest.raises(A2ATerminalEventUnavailable):
        project(raw)


def test_missing_and_legacy_producers_are_explicitly_unavailable() -> None:
    assert project(None) == {"status": "unavailable", "reason": "producer_unavailable"}
    raw = record()
    del raw["terminal_event"]
    assert project(raw)["reason"] == "producer_unavailable"
    raw["terminal_event"] = {"status": "unavailable", "reason": "legacy_unbound"}
    assert project(raw) == raw["terminal_event"]


@pytest.fixture
def authority(monkeypatch: pytest.MonkeyPatch) -> WorkItemA2AAuthority:
    instance = WorkItemA2AAuthority(lambda: object())
    item = {
        "status": "succeeded",
        "tenant": "tenant",
        "created_by": OWNER,
        "metadata": {
            "a2a_context_id": "a2a-context-" + "d" * 64,
            "a2a_owner_ref": OWNER,
        },
    }
    monkeypatch.setattr(instance, "_authorized_item", lambda *_args: item)
    monkeypatch.setattr(instance, "_session", lambda _scope: SESSION)
    monkeypatch.setattr(instance, "_owner_ref", lambda _session: OWNER)
    return instance


@pytest.mark.asyncio
async def test_shared_service_opt_in_reads_public_au_run_client(
    authority: WorkItemA2AAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []

    async def outcome(*, tenant: str, work_item_id: str) -> dict[str, Any]:
        assert tenant == SESSION.tenant
        calls.append(work_item_id)
        return record()

    client = SimpleNamespace(work_items=SimpleNamespace(get_outcome=outcome))
    engine = SimpleNamespace(
        graph_compute=SimpleNamespace(
            for_graph=lambda graph: SimpleNamespace(async_client=client)
        )
    )
    monkeypatch.setattr(authority, "_engine", lambda: (engine, engine))

    service = A2AService(
        authority=authority, router=OrchestratorA2ARouter(lambda: object())
    )
    ordinary = await service.get_task(TASK)
    assert calls == []
    assert ordinary is not None
    assert "terminalEvent" not in ordinary.metadata["arbora"]
    task = await _get_task(
        service, {"id": TASK, "includeCommittedEvent": True}, "request"
    )
    assert calls == ["workitem:orchestrator:" + TASK]
    assert task.metadata["arbora"]["terminalEvent"] == record()["terminal_event"]


@pytest.mark.asyncio
async def test_unauthorized_task_performs_zero_terminal_reads(
    authority: WorkItemA2AAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(authority, "_authorized_item", lambda *_args: None)
    monkeypatch.setattr(authority, "_engine", lambda: pytest.fail("producer accessed"))
    assert await authority.get(TASK, include_committed_event=True) is None


@pytest.mark.asyncio
async def test_post_read_authority_change_refuses_all_event_data(
    authority: WorkItemA2AAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arbora.a2a.terminal_event as terminal

    async def read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        monkeypatch.setattr(
            authority,
            "_session",
            lambda _scope: SimpleNamespace(tenant="foreign", graph="graph"),
        )
        return deepcopy(record()["terminal_event"])

    monkeypatch.setattr(terminal, "read_terminal_event", read)
    with pytest.raises(PermissionError):
        await authority.get(TASK, include_committed_event=True)


@pytest.mark.asyncio
async def test_http_committed_read_and_refusal_are_privacy_safe(
    authority: WorkItemA2AAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    import arbora.a2a.terminal_event as terminal
    from arbora.a2a.application import create_a2a_application

    class Auth:
        async def authenticate(self, request: Any, *, scope: str) -> None:
            if request.headers.get("Authorization") != "Bearer fixture":
                raise PermissionError("refused")

    async def read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return record()["terminal_event"]

    monkeypatch.setattr(terminal, "read_terminal_event", read)
    app = create_a2a_application(
        service=A2AService(
            authority=authority, router=OrchestratorA2ARouter(lambda: object())
        ),
        authenticator=Auth(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": "read",
            "method": "tasks/get",
            "params": {"id": TASK, "includeCommittedEvent": True},
        }
        response = await client.post(
            "/a2a", json=body, headers={"Authorization": "Bearer fixture"}
        )
        assert response.status_code == 200
        assert (
            response.json()["result"]["metadata"]["arbora"]["terminalEvent"]
            == record()["terminal_event"]
        )

        async def unavailable(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise A2ATerminalEventUnavailable("committed terminal event is unavailable")

        monkeypatch.setattr(terminal, "read_terminal_event", unavailable)
        response = await client.post(
            "/a2a", json=body, headers={"Authorization": "Bearer fixture"}
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == -32013
        assert "batch-1" not in response.text
        body["params"]["includeCommittedEvent"] = "true"
        response = await client.post(
            "/a2a", json=body, headers={"Authorization": "Bearer fixture"}
        )
        assert response.status_code == 400


@pytest.mark.parametrize("kind", ["outcome", "tool_call", "degraded", "reconciled"])
def test_actual_native_event_kinds_and_storage_owner_are_preserved(kind: str) -> None:
    value = record()
    value["terminal_event"]["kind"] = kind
    value["terminal_event"]["result_ref"] = "r" * 4096
    value["work_item"]["status"] = "dead_letter"
    value["terminal_event"]["outcome"] = "failed"
    event = project(value)
    assert event["kind"] == kind
    assert event["scope"]["tenant"] == "physical-owner"
    assert event["tenant"] == "tenant"
    assert event["outcome"] == "failed"
    assert len(event["result_ref"]) == 4096


@pytest.mark.asyncio
async def test_session_change_during_initial_lookup_makes_zero_producer_reads(
    authority: WorkItemA2AAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lookup(*args: Any) -> dict[str, Any]:
        monkeypatch.setattr(
            authority,
            "_session",
            lambda _scope: SimpleNamespace(tenant="foreign", graph="graph"),
        )
        return {
            "status": "succeeded",
            "tenant": "tenant",
            "created_by": OWNER,
            "metadata": {
                "a2a_context_id": "a2a-context-" + "d" * 64,
                "a2a_owner_ref": OWNER,
            },
        }

    monkeypatch.setattr(authority, "_authorized_item", lookup)
    monkeypatch.setattr(authority, "_engine", lambda: pytest.fail("producer accessed"))
    with pytest.raises(PermissionError):
        await authority.get(TASK, include_committed_event=True)
