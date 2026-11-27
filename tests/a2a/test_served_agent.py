"""The served agent keeps and lists caller conversations (ARBORA-A2A-R001.3).

The engine KV is a stateful in-memory double shared by every host in a test, so a
"restart" is a new storage object over the same entries: nothing is kept in process.
Time is a controlled clock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from arbora import host_bindings
from arbora.a2a.storage import (
    MAX_PAGE,
    A2AContextNotFound,
    EngineA2AStorage,
)
from arbora.core.settings import RuntimeConfig
from arbora.mcp_server.host_caches import ToolTable
from arbora.security.key_cache import JwksCache
from arbora.security.process_authority import ProcessAuthority

TENANT = "tenant:one"


class Kv:
    """``KvGet``, ``KvCas`` and ``KvScan`` with the engine's version semantics."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[bytes, int]] = {}

    async def __call__(
        self, method: str, params: dict[str, Any], key: str | None
    ) -> Any:
        assert params["namespace"] == "a2a"
        if method == "KvGet":
            row = self.rows.get(params["key"])
            return {"value": row[0], "version": row[1]} if row else {}
        if method == "KvCas":
            _value, version = self.rows.get(params["key"], (b"", 0))
            if version != params["expected_version"]:
                return {"swapped": False, "version": version}
            self.rows[params["key"]] = (params["new"], version + 1)
            return {"swapped": True, "version": version + 1}
        assert method == "KvScan"
        keys = sorted(k for k in self.rows if k.startswith(params["prefix"]))
        return {
            "entries": [{"key": k, "value": self.rows[k][0]} for k in keys],
            "next_cursor": None,
        }


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        self.now += 1
        return self.now


def host(
    kv: Kv, principal: str, clock: Callable[[], float], **kwargs: Any
) -> EngineA2AStorage:
    return EngineA2AStorage(
        kv, tenant=lambda: TENANT, principal=lambda: principal, clock=clock, **kwargs
    )


def message(text: str) -> Any:
    return {
        "role": "user",
        "parts": [{"kind": "text", "text": text}],
        "kind": "message",
        "message_id": f"m-{text[:8]}",
    }


@pytest.mark.spec("ARBORA-A2A-R001.3")
def test_context_survives_restart_and_list_is_caller_scoped() -> None:
    kv, clock = Kv(), Clock()

    async def scenario() -> None:
        first = host(kv, "person:one", clock)
        task = await first.submit_task("ctx-1", message("x" * 200))
        await first.update_context("ctx-1", [{"turn": 1}])
        await first.update_task(task["id"], "completed")
        # A restart: a new object over the same engine entries continues the context.
        second = host(kv, "person:one", clock)
        assert await second.load_context("ctx-1") == [{"turn": 1}]
        again = await second.submit_task("ctx-1", message("second turn"))
        assert again["context_id"] == "ctx-1"
        loaded = await second.load_task(task["id"])
        assert loaded is not None and loaded["status"]["state"] == "completed"
        # The title is the first user text, cut to 80 characters.
        await second.submit_task("ctx-2", message("another"))
        listed = await second.list_contexts()
        assert [c["contextId"] for c in listed["contexts"]] == ["ctx-2", "ctx-1"]
        assert listed["contexts"][1]["title"] == "x" * 80
        # Another principal sees none of it and cannot name the context.
        other = host(kv, "person:two", clock)
        assert (await other.list_contexts())["contexts"] == []
        assert await other.load_context("ctx-1") is None
        assert await other.load_task(task["id"]) is None
        with pytest.raises(A2AContextNotFound) as foreign:
            await other.submit_task("ctx-1", message("steal"))
        assert foreign.value.code == -32001

    asyncio.run(scenario())


@pytest.mark.spec("ARBORA-A2A-R001.3")
def test_list_pages_hold_fifty_entries_at_most_one_hundred_and_expire() -> None:
    kv, clock = Kv(), Clock()

    async def scenario() -> None:
        store = host(kv, "person:one", clock, retention_days=1)
        for index in range(120):
            await store.update_context(f"ctx-{index:03d}", [])
        page = await store.list_contexts()
        assert len(page["contexts"]) == 50 and page["next_cursor"]
        second = await store.list_contexts(cursor=page["next_cursor"])
        assert len(second["contexts"]) == 50
        # No context repeats across pages and the order is newest first.
        seen = [c["contextId"] for c in page["contexts"] + second["contexts"]]
        assert len(set(seen)) == 100 and seen[0] == "ctx-119"
        assert len((await store.list_contexts(limit=500))["contexts"]) == MAX_PAGE
        # Retention: a context past its window is no longer listed.
        clock.now += 2 * 86_400
        assert (await store.list_contexts())["contexts"] == []

    asyncio.run(scenario())


@pytest.mark.spec("ARBORA-A2A-R001.3")
def test_the_host_mounts_engine_storage_not_process_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(app=SimpleNamespace())

    import arbora.a2a.sdk_app as sdk_app

    monkeypatch.setattr(sdk_app, "create_admitted_a2a_app", create)
    monkeypatch.setattr(host_bindings, "fleet_agent", lambda build: object())
    monkeypatch.setattr(
        host_bindings, "prompt_normalizer", lambda _engine, _verbs: object()
    )
    import arbora.modality.agent_input as agent_input

    monkeypatch.setattr(agent_input, "MediaNormalizingAgent", lambda *a: object())
    mcp = SimpleNamespace(
        custom_route=lambda *a, **k: lambda fn: fn, add_extension=lambda e: None
    )
    host_bindings.mount_sdk_a2a(
        mcp,
        ToolTable(ProcessAuthority(config=RuntimeConfig())),
        build_toolset=lambda: None,
        engine_provider=lambda: None,
        keys=JwksCache(RuntimeConfig()),
    )
    assert isinstance(captured["storage"], EngineA2AStorage)


class _Caller:
    """Verifies the ``x-principal`` header; a request with none is refused."""

    async def authenticate(self, request: Any, *, scope: str) -> Any:
        return request.headers.get("x-principal") or None


def _served_app(kv: Kv) -> Any:
    """The real SDK application behind the guard, over engine storage on ``kv``."""
    from contextlib import contextmanager
    from contextvars import ContextVar

    from fasta2a.broker import InMemoryBroker

    from arbora.a2a.sdk_app import create_admitted_a2a_app

    class SchedulesNothing(InMemoryBroker):
        """The in-flight run is not under test: only admission and storage are."""

        async def run_task(self, params: Any) -> None:
            return None

    current: ContextVar[str] = ContextVar("principal")

    @contextmanager
    def bind(caller: Any) -> Any:
        token = current.set(str(caller))
        try:
            yield
        finally:
            current.reset(token)

    storage = EngineA2AStorage(
        kv, tenant=lambda: TENANT, principal=lambda: current.get(), clock=Clock()
    )
    return create_admitted_a2a_app(
        object(),
        authenticator=_Caller(),
        bind_caller=bind,
        name="arbora",
        url="http://testserver/a2a",
        version="0",
        storage=storage,
        broker=SchedulesNothing(),
        list_contexts=storage.list_contexts,
    )


def _rpc(client: Any, principal: str, method: str, params: dict[str, Any]) -> Any:
    return client.post(
        "/",
        headers={"x-principal": principal},
        json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params},
    )


@pytest.mark.spec("ARBORA-A2A-R001.3")
def test_served_tasks_list_is_caller_scoped_and_a_foreign_context_is_32001() -> None:
    from starlette.testclient import TestClient

    kv = Kv()
    served = _served_app(kv)
    with TestClient(served, raise_server_exceptions=False) as client:
        sent = _rpc(
            client,
            "person:a",
            "message/send",
            {
                "message": message("plan my week")
                | {"contextId": "ctx-a", "messageId": "m-wire"}
            },
        )
        assert sent.status_code == 200 and "error" not in sent.json()
        mine = _rpc(client, "person:a", "tasks/list", {}).json()["result"]
        assert [c["contextId"] for c in mine["contexts"]] == ["ctx-a"]
        assert mine["contexts"][0]["title"] == "plan my week"
        # Another caller sees none of it, and cannot continue it: -32001.
        assert (
            _rpc(client, "person:b", "tasks/list", {}).json()["result"]["contexts"]
            == []
        )
        foreign = _rpc(
            client,
            "person:b",
            "message/send",
            {"message": message("hi") | {"contextId": "ctx-a", "messageId": "m-wire"}},
        )
        assert foreign.json()["error"]["code"] == -32001
        # An unauthenticated caller never reaches the list.
        assert (
            client.post(
                "/", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/list"}
            ).status_code
            == 401
        )
        bad = _rpc(client, "person:a", "tasks/list", {"limit": "many"})
        assert bad.json()["error"]["code"] == -32602


@pytest.mark.spec("ARBORA-A2A-R001.3")
def test_the_host_hands_the_storage_list_to_the_served_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    import arbora.a2a.sdk_app as sdk_app

    monkeypatch.setattr(
        sdk_app,
        "create_admitted_a2a_app",
        lambda **kwargs: (
            captured.update(kwargs) or SimpleNamespace(app=SimpleNamespace())
        ),
    )
    monkeypatch.setattr(host_bindings, "fleet_agent", lambda build: object())
    monkeypatch.setattr(
        host_bindings, "prompt_normalizer", lambda _engine, _verbs: object()
    )
    import arbora.modality.agent_input as agent_input

    monkeypatch.setattr(agent_input, "MediaNormalizingAgent", lambda *a: object())
    mcp = SimpleNamespace(
        custom_route=lambda *a, **k: lambda fn: fn, add_extension=lambda e: None
    )
    host_bindings.mount_sdk_a2a(
        mcp,
        ToolTable(ProcessAuthority(config=RuntimeConfig())),
        build_toolset=lambda: None,
        engine_provider=lambda: None,
        keys=JwksCache(RuntimeConfig()),
    )
    assert captured["list_contexts"].__self__ is captured["storage"]
