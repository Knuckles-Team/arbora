"""The served host starts the tool-task consumer and it runs queued jobs (ARBORA-A2A-R002.2.3.2).

The engine here is a stateful in-memory WorkItem store behind the real queue, fence,
request store and dispatcher; only the engine calls and the invoke pipeline are
controlled. Time is the injected clock and waiting is injected: nothing sleeps.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from arbora import work_items
from arbora.a2a import tool_task_dispatch, tool_task_requests
from arbora.a2a.tool_task import ToolTaskRequest, tool_task_work_item_id
from arbora.a2a.tool_task_requests import EgToolTaskRequestStore
from arbora.mcp_server.served_invocation import _tool_task_service
from arbora.security.actor_context import ActorContext, ActorType, CredentialLease
from arbora.security.operation_result import OpError, OpResult
from arbora.security.session import GraphSession, use_session

TENANT = "tenant:one"
ORIGINAL = "person:one"
REQUEST = ToolTaskRequest(
    operation_id="graph.query",
    parameters_json='{"q":"x"}',
    principal=ORIGINAL,
    tenant=TENANT,
    session_id="session-1",
    authority_ref="delegation:1",
    policy_revision="p1",
    registry_digest="a" * 64,
)


def _session(actor_id: str) -> GraphSession:
    actor = ActorContext(
        actor_id=actor_id,
        actor_type=ActorType.SYSTEM,
        tenant_id=TENANT,
        authenticated=True,
        credential_lease=CredentialLease(int(time.time()) + 3600),
    )
    return GraphSession(
        actor=actor,
        tenant=TENANT,
        scopes=frozenset({"kg:read", "kg:write"}),
        policy_version="policy:one",
        audience="arbora",
    )


HOST = _session("service:arbora")
CALLER = _session(ORIGINAL)


class Blob:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def store(self, raw: bytes, *, chunk_size: int) -> str:
        digest = f"{len(self.blobs):064x}"
        self.blobs[digest] = raw
        return digest

    async def incref(self, digest: str) -> int:
        return 1

    async def fetch_begin(self, digest: str) -> tuple[str, int]:
        return digest, 1

    async def chunk_get(self, cursor: str, index: int) -> bytes:
        return self.blobs[cursor]

    async def fetch_end(self, cursor: str) -> bool:
        return True


class Engine:
    """The engine: blobs and WorkItems that outlive every host that polls them."""

    def __init__(self) -> None:
        self.blob = Blob()
        self.items: dict[str, dict[str, Any]] = {}
        self.graph_compute = SimpleNamespace(
            for_graph=lambda _graph: SimpleNamespace(
                async_client=SimpleNamespace(blob=self.blob)
            )
        )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (
            "submit_work_item_atomic",
            "get_work_item",
            "list_work_items",
            "claim_specific",
            "heartbeat",
            "mark_effect_started",
            "commit_result",
        ):
            monkeypatch.setattr(work_items, name, getattr(self, name))

    def submit_work_item_atomic(self, _engine: Any, **kwargs: Any) -> tuple[str, bool]:
        item_id = kwargs["work_item_id"]
        self.items.setdefault(
            item_id,
            {
                "work_item_id": item_id,
                "kind": kwargs["kind"],
                "input_ref": kwargs["payload_ref"],
                "metadata": kwargs["metadata"],
                "status": "ready",
                "effect_phase": "unstarted",
                "version": 1,
            },
        )
        return item_id, True

    def get_work_item(self, _engine: Any, item_id: str) -> dict[str, Any] | None:
        item = self.items.get(item_id)
        return dict(item) if item else None

    def list_work_items(self, _engine: Any, *, kind: str, **_: Any) -> dict[str, Any]:
        rows = [dict(i) for i in self.items.values() if i["kind"] == kind]
        return {"items": rows, "next_cursor": None}

    def claim_specific(
        self, _engine: Any, item_id: str, *, token: str, lease_ttl_s: float
    ) -> dict[str, Any] | None:
        item = self.items[item_id]
        if item["status"] != "ready":
            return None
        item["status"] = "leased"
        return {
            "work_item_id": item_id,
            "tenant": TENANT,
            "lease_holder_ref": token,
            "lease_epoch": 1,
            "fencing_token": 1,
            "attempt": 1,
            "payload_ref": item["input_ref"],
        }

    def heartbeat(self, *_: Any, **__: Any) -> bool:
        return True

    def mark_effect_started(
        self, _engine: Any, item_id: str, claim: Any, *, expected_revision: int
    ) -> dict[str, Any]:
        item = self.items[item_id]
        assert item["version"] == expected_revision
        item["effect_phase"] = "started"
        item["version"] += 1
        return {}

    def commit_result(
        self,
        _engine: Any,
        item_id: str,
        claim: Any,
        *,
        outcome: str,
        result_ref: str | None = None,
        error_ref: str | None = None,
        **_: Any,
    ) -> str:
        item = self.items[item_id]
        item.update(
            status=outcome,
            effect_phase="resolved",
            result_ref=result_ref,
            error_ref=error_ref,
        )
        return "committed"


class Host:
    """One host replica: its own consumer over the shared engine."""

    def __init__(self, engine: Engine, invocations: list[Any], revoked: bool = False):
        self.invocations = invocations
        self.crash = False
        self.refuse = False

        async def invoke(tenant: str, op_id: str, params: Any, key: str) -> Any:
            self.invocations.append((tenant, op_id, dict(params), key))
            if self.crash:
                raise ConnectionError("connector died after the effect")
            if self.refuse:
                return OpError("FORBIDDEN")
            return OpResult(value={"rows": 1})

        async def read(delegation_id: str) -> Any:
            if revoked:
                return None
            assert delegation_id == REQUEST.authority_ref
            return {
                "standing": "active",
                "principal_active": True,
                "holds_tool_scope": True,
                "record": {
                    "tenant": TENANT,
                    "principal": ORIGINAL,
                    "tool": REQUEST.operation_id,
                    "revoked_at": None,
                },
            }

        runtime = SimpleNamespace(
            bindings={
                "a2a": SimpleNamespace(
                    authority=SimpleNamespace(
                        engine=lambda: engine, delegation_reader=read
                    )
                )
            }
        )

        async def no_wait(_seconds: float) -> None:
            await asyncio.sleep(0)

        self.probes: dict[str, Any] = {}
        self.service = _tool_task_service(
            runtime,
            lambda: HOST,
            SimpleNamespace(invoke=invoke),
            self.probes,
            sleep=no_wait,
        )[0]

    async def poll(self) -> list[Any]:
        """Run one poll of the real consumer on its thread, as the host loop does."""
        self.service._loops[:] = [asyncio.get_running_loop()]
        return await asyncio.to_thread(self.service.consumer._poll_once)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    engine = Engine()
    engine.install(monkeypatch)
    monkeypatch.setattr(
        tool_task_requests, "resolve_session", lambda **_: use_session_value()
    )
    monkeypatch.setattr(
        tool_task_dispatch, "resolve_session", lambda **_: use_session_value()
    )
    monkeypatch.delenv("ARBORA_A2A_TOOL_TASK_POLL_S", raising=False)
    yield engine


def use_session_value() -> GraphSession:
    from arbora.security.session import current_session

    session = current_session()
    assert session is not None
    return session


def admit(engine: Engine, job: str = "job-1") -> None:
    """The original caller admits the job: request bytes and one WorkItem."""
    with use_session(CALLER):
        EgToolTaskRequestStore(engine).store(job, REQUEST)


@pytest.mark.spec("ARBORA-A2A-R002.2.3.2")
def test_bootstrap_starts_consumer_and_reports_ready(world: Engine) -> None:
    async def scenario() -> None:
        host = Host(world, [])
        assert host.service is not None
        # Before the consumer polls, /ready reports it UNAVAILABLE; once it runs, ok.
        assert host.probes["tool_task_consumer"](None) == {
            "status": "unhealthy",
            "reason": "UNAVAILABLE",
        }
        stop = False
        runner = asyncio.create_task(host.service.run(lambda: stop))
        for _ in range(50):
            if host.service.running:
                break
            await asyncio.sleep(0)
        probe = host.probes["tool_task_consumer"](None)
        assert probe["status"] == "ok"
        stop = True
        await runner
        assert not host.service.running
        assert host.probes["tool_task_consumer"](None)["reason"] == "UNAVAILABLE"

    asyncio.run(scenario())


@pytest.mark.spec("ARBORA-A2A-R002.2.3")
@pytest.mark.spec("ARBORA-A2A-R002.2.3.2")
def test_bootstrap_starts_consumer_and_recovers_after_restart(world: Engine) -> None:
    invocations: list[Any] = []
    admit(world)

    async def scenario() -> None:
        first = Host(world, invocations)
        # A replica delivers the queued job once, as the service caller, keyed by job id.
        with use_session(HOST):
            await first.poll()
        assert invocations == [(TENANT, "graph.query", {"q": "x"}, "job-1")]
        item = world.items[tool_task_work_item_id("job-1")]
        assert item["status"] == "succeeded"
        assert item["result_ref"].startswith("tool-task-result:sha256:")

        # The host restarts: a fresh replica holds no state and finds the job settled.
        with use_session(HOST):
            restarted = Host(world, invocations)
            await restarted.poll()
        assert len(invocations) == 1

    asyncio.run(scenario())


@pytest.mark.spec("ARBORA-A2A-R002.2.3")
@pytest.mark.spec("ARBORA-A2A-R002.2.3.2")
def test_revoked_caller_is_recovered_without_credentials_and_no_effect(
    world: Engine,
) -> None:
    invocations: list[Any] = []
    admit(world)

    async def scenario() -> None:
        with use_session(HOST):
            host = Host(world, invocations, revoked=True)
            await host.poll()

    asyncio.run(scenario())
    item = world.items[tool_task_work_item_id("job-1")]
    assert invocations == [] and item["status"] == "failed"
    # Native request bytes and the WorkItem hold no credential: only references.
    assert "token" not in str(item["metadata"]).lower()


@pytest.mark.spec("ARBORA-A2A-R002.2.3")
@pytest.mark.spec("ARBORA-A2A-R002.2.3.2")
def test_an_uncertain_effect_stays_open_and_a_restart_never_repeats_it(
    world: Engine,
) -> None:
    invocations: list[Any] = []
    admit(world)

    async def scenario() -> None:
        with use_session(HOST):
            crashed = Host(world, invocations)
            crashed.crash = True
            await crashed.poll()
            restarted = Host(world, invocations)
            await restarted.poll()

    asyncio.run(scenario())
    item = world.items[tool_task_work_item_id("job-1")]
    assert len(invocations) == 1
    assert item["status"] == "leased" and item["effect_phase"] == "started"


@pytest.mark.spec("ARBORA-A2A-R002.2.3.2")
def test_a_refusal_before_dispatch_commits_failed(world: Engine) -> None:
    invocations: list[Any] = []
    admit(world)

    async def scenario() -> None:
        with use_session(HOST):
            host = Host(world, invocations)
            host.refuse = True
            await host.poll()

    asyncio.run(scenario())
    item = world.items[tool_task_work_item_id("job-1")]
    assert item["status"] == "failed" and "refused:FORBIDDEN" in item["error_ref"]


@pytest.mark.spec("ARBORA-A2A-R002.2.3.2")
def test_a_host_without_the_engine_client_starts_none_and_reports_unavailable(
    world: Engine,
) -> None:
    runtime = SimpleNamespace(
        bindings={"a2a": SimpleNamespace(authority=SimpleNamespace())}
    )
    probes: dict[str, Any] = {}
    assert _tool_task_service(runtime, lambda: HOST, SimpleNamespace(), probes) == ()
    assert probes["tool_task_consumer"](None) == {
        "status": "unhealthy",
        "reason": "UNAVAILABLE",
    }
    # A host that serves no A2A reports nothing about the consumer.
    none: dict[str, Any] = {}
    assert (
        _tool_task_service(SimpleNamespace(bindings={}), lambda: HOST, None, none) == ()
    )
    assert none == {}
