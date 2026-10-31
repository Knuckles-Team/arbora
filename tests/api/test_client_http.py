"""The HTTP transport for arbora.client (ARBORA-OPS-R067.3)."""

from __future__ import annotations

import json

import httpx
import pytest

from arbora.client._models import registry_digest
from arbora.client.http import HttpTransport, OperationError
from arbora.client.invoke import invoke

# spec: ARBORA-OPS-R025
# spec: ARBORA-OPS-R025.1
# spec: ARBORA-HOST-R001.3


def _transport(
    status: int, body: dict
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler), seen


@pytest.mark.spec("ARBORA-OPS-R067.3")
async def test_invoke_posts_the_operation_with_a_bearer_token() -> None:
    mock, seen = _transport(
        200,
        {
            "ok": True,
            "result": {"value": {}},
            "meta": {"registry_digest": registry_digest()},
        },
    )
    http = HttpTransport("https://arbora.example", "tok", transport=mock)
    result = await invoke(http, "research.web", {"query": "pdb"})
    assert result["ok"] is True
    (request,) = seen
    assert request.url.path == "/api/v1/ops/research.web"
    assert request.headers["Authorization"] == "Bearer tok"
    assert json.loads(request.content) == {"query": "pdb"}


@pytest.mark.spec("ARBORA-OPS-R067.3")
async def test_refusal_raises_the_operation_code() -> None:
    mock, _ = _transport(
        403,
        {
            "ok": False,
            "error": {"code": "ACCESS_DENIED"},
            "meta": {"registry_digest": registry_digest()},
        },
    )
    http = HttpTransport("https://arbora.example", "tok", transport=mock)
    with pytest.raises(OperationError) as refused:
        await invoke(http, "research.web", {"query": "pdb"})
    assert refused.value.code == "ACCESS_DENIED"
    with pytest.raises(ValueError):
        HttpTransport("arbora.example", "tok")
