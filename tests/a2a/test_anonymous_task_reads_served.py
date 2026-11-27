"""Native anonymous task ownership served through the none-mode read application."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import httpx
import pytest
from medulla.client import MedullaClient

from arbora.a2a.anonymous import AnonymousReadPolicy, create_anonymous_a2a_application
from arbora.a2a.anonymous_native import compose_anonymous_task_reads
from arbora.a2a.service import A2AService
from tests.a2a.test_anonymous_native_reads import (
    RUN,
    LocalWriter,
    response,
)

ORIGIN = "http://127.0.0.1:8080"
POLICY = AnonymousReadPolicy(ORIGIN, "local", "revision-1", frozenset({"tasks/get"}))


def _client(value: Any, *, mode: str = "none", supported: bool = True) -> Any:
    client = MedullaClient(
        asyncio.StreamReader(), LocalWriter(), None, "default", auth_mode="none"
    )
    client._auth_mode = mode
    sent: list[Any] = []

    async def send(method: str, params: Any, graph: str, *, idempotency_key: Any):
        sent.append((method, params, graph))
        return copy.deepcopy(value)

    async def supports(method: str) -> bool:
        return supported and method == "GetRunWorkItem"

    client._send = send
    client.supports = supports
    return client, sent


async def _read(owner: Any, task: str = RUN) -> httpx.Response:
    app = create_anonymous_a2a_application(
        owner=owner, service=A2AService(authority=owner, router=None)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
    ) as http:
        return await http.post(
            "/a2a",
            headers={"Origin": ORIGIN, "X-Arbora-Anonymous": "1"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tasks/get",
                "params": {"id": task},
            },
        )


@pytest.mark.spec("ARBORA-IDENTITY-R004.8.2")
@pytest.mark.asyncio
async def test_native_owner_checks_anonymous_tasks() -> None:
    audit: list[dict[str, str]] = []
    client, sent = _client(response())
    owner = await compose_anonymous_task_reads(
        client, graph="control", policy=POLICY, audit=audit.append
    )
    assert owner is not None
    # The engine owner check answers: the anonymous reader sees its own task.
    served = await _read(owner)
    assert served.status_code == 200 and served.json()["result"]["id"] == RUN
    assert sent[0][0] == "GetRunWorkItem"

    # A task the engine does not bind to the anonymous owner answers as absent.
    missing, _ = _client(None)
    absent = await compose_anonymous_task_reads(
        missing, graph="control", policy=POLICY, audit=audit.append
    )
    assert absent is not None and (await _read(absent)).status_code == 404

    # Each refusal is typed and audited with the task id and the reason code only.
    for mutate, code in (
        (lambda p: p["admission"].update(run_id="foreign"), "ANON_OWNER_MISMATCH"),
        (
            lambda p: p["admission"]["context"].update(graph="other"),
            "ANON_NAMESPACE_MISMATCH",
        ),
        (
            lambda p: p["admission"]["context"].update(tenant_id="other"),
            "ANON_NAMESPACE_MISMATCH",
        ),
    ):
        payload = response()
        mutate(payload)
        bad, _ = _client(payload)
        owner_bad = await compose_anonymous_task_reads(
            bad, graph="control", policy=POLICY, audit=audit.append
        )
        assert owner_bad is not None
        assert (await _read(owner_bad)).status_code == 403
        assert audit[-1] == {"task_id": RUN, "reason": code}

    # A client that leaves none mode mid-request is refused as substituted.
    swapped, _ = _client(response())
    owner_swapped = await compose_anonymous_task_reads(
        swapped, graph="control", policy=POLICY, audit=audit.append
    )
    assert owner_swapped is not None
    original = swapped._send

    async def leaves_none(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        swapped._auth_mode = "authenticated"
        return result

    swapped._send = leaves_none
    assert (await _read(owner_swapped)).status_code == 403
    assert audit[-1] == {"task_id": RUN, "reason": "ANON_CLIENT_SUBSTITUTED"}
    assert all(set(event) == {"task_id", "reason"} for event in audit)


@pytest.mark.spec("ARBORA-IDENTITY-R004.8.2")
@pytest.mark.asyncio
async def test_the_owner_composes_only_in_none_mode_after_support_is_reported() -> None:
    authenticated, sent = _client(response(), mode="authenticated")
    assert (
        await compose_anonymous_task_reads(
            authenticated, graph="control", policy=POLICY
        )
        is None
    )
    unsupported, sent_unsupported = _client(response(), supported=False)
    assert (
        await compose_anonymous_task_reads(unsupported, graph="control", policy=POLICY)
        is None
    )
    assert sent == [] and sent_unsupported == []
