"""The served host mounts the none-mode anonymous task owner (ARBORA-IDENTITY-R004.8.2).

``register_a2a_tools`` is the one path the host mounts ``POST /a2a`` on, reached over
the real ASGI application of the served FastMCP.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastmcp import FastMCP

from arbora.a2a.anonymous_host import anonymous_a2a_provider
from arbora.a2a.mcp import register_a2a_tools
from arbora.a2a.service import A2AService
from arbora.core.settings import RuntimeConfig
from arbora.security import anonymous
from arbora.security.key_cache import JwksCache
from tests.a2a.test_anonymous_native_reads import RUN, response
from tests.a2a.test_anonymous_task_reads_served import ORIGIN, _client

BODY = {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": RUN}}
HEADERS = {
    "Origin": ORIGIN,
    "X-Arbora-Anonymous": "1",
    "Content-Type": "application/json",
}


def _host(client: Any) -> FastMCP:
    async def connect() -> Any:
        return client

    mcp = FastMCP("arbora-test")
    register_a2a_tools(
        mcp,
        service=A2AService(authority=None, router=None, config=RuntimeConfig()),  # type: ignore[arg-type]
        tool_table={},
        keys=JwksCache(RuntimeConfig()),
        anonymous=anonymous_a2a_provider(
            origin=ORIGIN,
            graph="control",
            revision="test",
            connect=connect,
            config=RuntimeConfig(),
        ),
    )
    return mcp


async def _post(mcp: FastMCP) -> httpx.Response:
    app = mcp.http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
    ) as http:
        return await http.post("/a2a", headers=HEADERS, json=BODY)


@pytest.mark.spec("ARBORA-IDENTITY-R004.8.2")
@pytest.mark.asyncio
async def test_the_host_serves_an_anonymous_task_read_over_a2a_in_none_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anonymous, "_ACTIVE_MODE", "none")
    client, sent = _client(response())
    served = await _post(_host(client))
    assert served.status_code == 200 and served.json()["result"]["id"] == RUN
    assert sent[0][0] == "GetRunWorkItem"


@pytest.mark.spec("ARBORA-IDENTITY-R004.8.2")
@pytest.mark.asyncio
async def test_without_native_support_the_anonymous_application_does_not_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anonymous, "_ACTIVE_MODE", "none")
    client, sent = _client(response(), supported=False)
    served = await _post(_host(client))
    assert sent == []
    assert served.status_code in {401, 403}
