"""The host composition mounts the none-mode anonymous task owner (ARBORA-IDENTITY-R004.8.2)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from arbora.a2a.anonymous import AnonymousReadPolicy
from arbora.a2a.composition import compose_a2a, compose_anonymous_a2a
from arbora.run_origin import RunOriginRegistry
from tests.a2a.test_anonymous_native_reads import RUN, response
from tests.a2a.test_anonymous_task_reads_served import ORIGIN, POLICY, _client
from tests.a2a.test_application import Authenticator


async def _compose(client: Any) -> Any:
    anonymous = await compose_anonymous_a2a(client, graph="control", policy=POLICY)
    return compose_a2a(
        engine_provider=lambda: pytest.fail("engine was accessed"),
        origins=RunOriginRegistry(),
        authenticator=Authenticator(),  # type: ignore[arg-type]
        anonymous_application=anonymous,
    )


@pytest.mark.spec("ARBORA-IDENTITY-R004.8.2")
@pytest.mark.asyncio
async def test_the_host_composition_serves_anonymous_task_reads_through_a2a() -> None:
    client, sent = _client(response())
    composition = await _compose(client)
    assert composition.anonymous_application is not None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=composition.anonymous_application),
        base_url=ORIGIN,
    ) as http:
        served = await http.post(
            "/a2a",
            headers={"Origin": ORIGIN, "X-Arbora-Anonymous": "1"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tasks/get",
                "params": {"id": RUN},
            },
        )
    assert served.status_code == 200 and served.json()["result"]["id"] == RUN
    assert sent[0][0] == "GetRunWorkItem"
    assert isinstance(POLICY, AnonymousReadPolicy)


@pytest.mark.spec("ARBORA-IDENTITY-R004.8.2")
@pytest.mark.asyncio
async def test_the_host_composition_mounts_nothing_without_native_support() -> None:
    for client, _ in (
        _client(response(), mode="authenticated"),
        _client(response(), supported=False),
    ):
        assert (await _compose(client)).anonymous_application is None
