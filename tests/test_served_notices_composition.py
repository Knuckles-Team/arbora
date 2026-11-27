"""The served composition binds the digest channel and the outbox consumers.

``compose_served_invocation`` binds ``approval_digest_delivery`` (ARBORA-OPS-R918.5.1)
and one outbox runner for the trust-flag notice (ARBORA-OPS-R855.3.1) and the approver
notice (ARBORA-OPS-R918.6), and its start-up step starts the runner. Each path goes
through the composed bindings and the engine client, not through a module under test.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import msgpack
import pytest

from arbora.access.action_policy import ActionPolicies
from arbora.api.domain.fleet.outbox_consumer import TRUST_FLAG_TOPIC, OutboxRunner
from arbora.core.settings import RuntimeConfig, runtime_config
from arbora.fleet.approval_notifier import APPROVALS_TOPIC
from arbora.mcp_server.served_invocation import (
    compose_served_invocation,
    refused_fleet_effect,
)
from arbora.security import request_identity
from arbora.security.actor_context import ActorContext, ActorType
from arbora.security.key_cache import JwksCache
from tests.test_served_operation_api import FakeClient, FakePool

USERS = [
    {"principal_id": "p:ed", "roles": ["editors"], "status": "active"},
    {"principal_id": "p:boss", "roles": ["admin"], "status": "active"},
]


def _lease(topic: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "record": {
            "intent": {
                "topic": topic,
                "key": key,
                "headers": {},
                "payload": msgpack.packb(payload),
            }
        }
    }


class NoticeClient(FakeClient):
    """The engine fake: identity users, a stored-query read and a two-topic outbox."""

    def __init__(self) -> None:
        super().__init__()
        self.pending = {
            TRUST_FLAG_TOPIC: [
                _lease(TRUST_FLAG_TOPIC, "t1", {"asset": "a", "flag": "warned"})
            ],
            APPROVALS_TOPIC: [
                _lease(
                    APPROVALS_TOPIC,
                    "p1",
                    {
                        "approval_id": "approval:1",
                        "gate_class": "publishing",
                        "approver_roles": ["editors"],
                        "expires_at_ms": 9,
                    },
                )
            ],
        }
        self.acked: list[str] = []

    async def _send(
        self, method: str, params: Any, graph: str | None, **kw: Any
    ) -> Any:
        if method == "Identity":
            return {"kind": "users", "value": USERS}
        if method == "Uql":
            return {"rows": [{"principal": "p:author"}]}
        if method == "MutationOutbox":
            op = params["op"]
            if op["op"] == "pull":
                return {"leases": list(self.pending[op["topics"][0]])}
            self.acked.append(op["lease"]["record"]["intent"]["key"])
            return {}
        return await super()._send(method, params, graph, **kw)


@pytest.fixture
def pinned_with_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    from medulla.generated import models

    monkeypatch.setattr(models, "MutationOutboxOpPull", object, raising=False)
    monkeypatch.setattr(models, "MutationOutboxOpAck", object, raising=False)


@pytest.fixture
def reach(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The governed reach channel, recording each notice that leaves."""
    from types import SimpleNamespace

    sent: list[str] = []

    def reach_user_sync(text: str, **_kw: Any) -> Any:
        sent.append(text)
        return SimpleNamespace(success=True, error=None)

    monkeypatch.setattr("arbora.messaging.service.reach_user_sync", reach_user_sync)
    return sent


def _compose(client: NoticeClient, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(runtime_config, "auth_jwt_audience", "arbora-fixture")
    monkeypatch.setattr(runtime_config, "kg_policy_version", "policy:fixture")
    actor = ActorContext(
        actor_id="person:one",
        actor_type=ActorType.HUMAN,
        tenant_id="tenant:one",
        authenticated=True,
        roles=("kg:read",),
        credential_expires_at=int(time.time()) + 3600,
    )
    process = request_identity.mint_graph_session(actor)
    return compose_served_invocation(
        config=RuntimeConfig(),
        keys=JwksCache(),
        pool=FakePool(client),
        graph=process.graph,
        process_session=lambda: process,
        action_policies=ActionPolicies(),
        bindings={},
        fleet_effect=refused_fleet_effect,
    )


# spec: ARBORA-OPS-R918.5.1
def test_served_bindings_deliver_the_digest_to_the_owners(
    monkeypatch: pytest.MonkeyPatch, reach: list[str]
) -> None:
    invocation = _compose(NoticeClient(), monkeypatch)
    bindings = invocation.services.runtime.bindings
    by_class = [{"gate_class": "publishing", "pending": 2, "oldest_age_ms": 4}]

    delivered = asyncio.run(bindings["approval_digest_delivery"](by_class))

    assert delivered == 1
    assert reach == ["Approvals digest. publishing: 2 pending, oldest 4 ms"]


# spec: ARBORA-OPS-R855.3.1, ARBORA-OPS-R918.6
def test_served_runner_ticks_the_trust_flag_and_approver_consumers(
    monkeypatch: pytest.MonkeyPatch,
    pinned_with_outbox: None,
    reach: list[str],
) -> None:
    client = NoticeClient()
    invocation = _compose(client, monkeypatch)
    runner = invocation.services.runtime.bindings["outbox_runner"]

    done = asyncio.run(runner.run_once())

    assert done == {TRUST_FLAG_TOPIC: 1, APPROVALS_TOPIC: 1}
    assert sorted(client.acked) == ["p1", "t1"]
    assert any("Asset a was flagged warned" in text for text in reach)
    assert any("approval:1" in text and "publishing" in text for text in reach)


# spec: ARBORA-OPS-R918.6
def test_process_startup_starts_the_runner_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts: list[str] = []
    monkeypatch.setattr(OutboxRunner, "start", lambda self: starts.append("start"))
    invocation = _compose(NoticeClient(), monkeypatch)
    assert invocation.process_startup is not None

    asyncio.run(invocation.process_startup())

    assert starts == ["start"]


# spec: ARBORA-OPS-R918.6
def test_runner_schedule_uses_the_injected_wait_and_stops_on_request() -> None:
    ticks: list[int] = []
    waits: list[float] = []
    stopped = threading.Event()

    class Counting(OutboxRunner):
        async def run_once(self) -> dict[str, int]:
            ticks.append(1)
            return {}

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        if len(waits) == 3:
            stopped.set()
        return stopped.is_set()

    runner = Counting((), _never_send, graph="g", interval_s=7.0, wait=wait)
    assert runner.start() is True and runner.start() is False
    runner._thread.join(timeout=30)  # type: ignore[union-attr]
    assert ticks == [1, 1, 1] and waits == [7.0, 7.0, 7.0]
    runner.stop()
    assert runner.start() is False


async def _never_send(method: str, params: dict[str, Any]) -> Any:
    raise AssertionError("no consumer is registered")


# spec: ARBORA-OPS-R918.6
def test_a_failed_tick_is_logged_and_does_not_end_the_schedule(
    pinned_with_outbox: None, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(method: str, params: dict[str, Any]) -> Any:
        raise RuntimeError("engine down")

    from arbora.api.domain.fleet.outbox_consumer import OutboxConsumer

    async def handle(event: Any) -> None:
        raise AssertionError("never reached")

    runner = OutboxRunner(
        (OutboxConsumer("t", "c", handle),), broken, graph="g", interval_s=1.0
    )
    with caplog.at_level("ERROR"):
        asyncio.run(runner.run_once())
    assert "tick failed" in caplog.text
