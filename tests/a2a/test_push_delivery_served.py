"""Registered pushes survive a restart and dead-letter, through the served route (ARBORA-A2A-R013.4).

A caller registers two webhooks over ``POST /a2a``. The task reaches a terminal state and
the host stops with both pushes still pending. A restarted host (a new authority and
worker over the same engine) delivers the first once and takes the second, which always
fails, to ``PUSH_DEAD_LETTER`` after the configured attempts. Time is the injected clock.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arbora.a2a.application import create_a2a_application
from arbora.a2a.push import PUSH_KIND
from arbora.a2a.service import A2AService
from arbora.security import session as session_module
from tests.a2a._push_support import ITEM_ID, TASK_ID, Target, seed_task, world
from tests.a2a.test_application import Router

HEADERS = {"Authorization": "Bearer verified"}
GOOD = "https://good.example.test/hook"
BAD = "https://bad.example.test/hook"


class Authenticator:
    """Binds the host's verified session for the request, as the served door does."""

    def __init__(self, who: Any) -> None:
        self.who = who

    async def authenticate(self, request: Any, *, scope: str) -> None:
        session_module._current.set(self.who)


def _client(host: Any) -> TestClient:
    service = A2AService(
        authority=host.authority,  # type: ignore[arg-type]
        router=Router(),
        push=host.worker,
    )
    return TestClient(
        create_a2a_application(service=service, authenticator=Authenticator(host.who))
    )


def _set(client: TestClient, url: str, **extra: Any) -> Any:
    return client.post(
        "/a2a",
        headers=HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": "p",
            "method": "tasks/pushNotificationConfig/set",
            "params": {"id": TASK_ID, "url": url, **extra},
        },
    )


class _PostByHost(Target):
    """Answers 200 for the good receiver and 500 for the bad one."""

    def __call__(self, method: str, url: str, body: Any, headers: Any) -> int:
        super().__call__(method, url, body, headers)
        return 200 if url == GOOD else 500


@pytest.mark.spec("ARBORA-A2A-R013.4")
def test_push_survives_restart_and_dead_letters() -> None:
    first = world(post=_PostByHost(200), max_attempts=3)
    seed_task(first)
    client = _client(first)
    assert _set(client, GOOD, tokenRef="vault:a2a/token").status_code == 200
    assert _set(client, BAD).status_code == 200
    first.items.rows[ITEM_ID]["status"] = "succeeded"

    async def terminal() -> None:
        session_module._current.set(first.who)
        await first.authority._publish(
            ITEM_ID,
            None,
            task_id=TASK_ID,
            context_id="ctx",
            kind="status-update",
            state="completed",
            final=True,
        )

    asyncio.run(terminal())
    pending = [r for r in first.items.rows.values() if r["kind"] == PUSH_KIND]
    assert [r["status"] for r in pending] == ["ready", "ready"]
    assert first.worker._post.posts == []  # type: ignore[attr-defined]

    # The host stops here. A new process reads the queue from the engine alone.
    post = _PostByHost(200)
    second = world(first.items, clock=first.clock, post=post, max_attempts=3)
    asyncio.run(second.worker.run_once((second.who,)))
    for _ in range(2):
        second.clock.now += 600
        asyncio.run(second.worker.run_once((second.who,)))
        asyncio.run(second.worker.run_once((second.who,)))

    by_url = {r["metadata"]["url"]: r for r in pending}
    assert by_url[GOOD]["status"] == "succeeded"
    assert by_url[BAD]["status"] == "failed"
    assert by_url[BAD]["error_ref"] == "PUSH_DEAD_LETTER"
    delivered = [url for url, _headers, _body in post.posts]
    assert delivered.count(GOOD) == 1
    assert delivered.count(BAD) == 3


@pytest.mark.spec("ARBORA-A2A-R013.4")
def test_a_foreign_task_and_a_refused_target_are_typed_over_the_route() -> None:
    host = world()
    seed_task(host)
    client = _client(host)
    refused = _set(client, "http://good.example.test/hook")
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == -32602
    assert refused.json()["error"]["data"] == {"reason": "PUSH_URL_INVALID"}
    private = _set(client, "https://10.0.0.1/hook")
    assert private.json()["error"]["data"] == {"reason": "PUSH_TARGET_PRIVATE"}
    host.items.rows[ITEM_ID]["metadata"]["a2a_owner_ref"] = "someone-else"
    foreign = _set(client, GOOD)
    assert (foreign.status_code, foreign.json()["error"]["code"]) == (404, -32001)
    assert "a2a_push_targets" not in host.items.rows[ITEM_ID]["metadata"]
