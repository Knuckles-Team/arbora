"""The SDK A2A application is reachable only through Arbora admission."""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from arbora.a2a.sdk_app import AdmittedA2AApp

pytestmark = pytest.mark.asyncio


class _App:
    def __init__(self) -> None:
        self.seen: list[tuple[str, Any]] = []

    async def __call__(self, scope, receive, send) -> None:
        self.seen.append((scope["type"], bound[-1] if bound else None))
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})


bound: list[Any] = []


@contextlib.contextmanager
def _bind(caller: Any):
    bound.append(caller)
    try:
        yield
    finally:
        bound.pop()


class _Auth:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.scopes: list[str] = []

    async def authenticate(self, _request: Any, *, scope: str) -> Any:
        self.scopes.append(scope)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def _call(guard: AdmittedA2AApp, scope: dict[str, Any]) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await guard(scope, receive, send)
    return sent


def _http(method: str) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": "/",
        "headers": [],
        "query_string": b"",
    }


@pytest.mark.parametrize(
    ("method", "scope"), [("GET", "kg:read"), ("HEAD", "kg:read"), ("POST", "kg:write")]
)
async def test_admitted_caller_reaches_app_inside_its_binding(method, scope) -> None:
    app, auth = _App(), _Auth("caller-1")
    sent = await _call(AdmittedA2AApp(app, auth, bind_caller=_bind), _http(method))
    assert auth.scopes == [scope]
    assert app.seen == [("http", "caller-1")]
    assert sent[0]["status"] == 200


@pytest.mark.parametrize("result", [None, PermissionError("no scope")])
async def test_unverified_caller_gets_401_and_app_never_runs(result) -> None:
    app = _App()
    sent = await _call(AdmittedA2AApp(app, _Auth(result)), _http("POST"))
    assert app.seen == []
    assert sent[0]["status"] == 401
    assert b"A2A authority required" in sent[1]["body"]


async def test_lifespan_passes_through_without_admission() -> None:
    app, auth = _App(), _Auth(None)
    await _call(AdmittedA2AApp(app, auth), {"type": "lifespan"})
    assert app.seen == [("lifespan", None)] and auth.scopes == []
