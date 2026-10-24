"""The REST surface binds the authenticated caller's own session (ARBORA-OPS-R007.1)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from arbora.api.contracts.session import current_session
from arbora.api.http.routes import make_endpoint
from arbora.security.actor_context import current_actor


class _Actor(SimpleNamespace):
    def ensure_credential_current(self) -> None:
        return None


class _Session(SimpleNamespace):
    def __post_init__(self) -> None:
        return None

    def ensure_authority_current(self) -> None:
        return None


@pytest.mark.spec("ARBORA-OPS-R007.1")
def test_rest_invoke_runs_under_the_callers_verified_session() -> None:
    actor = _Actor(actor_id="user-1", tenant_id="t1", authenticated=True)
    session = _Session(actor=actor, tenant="t1")
    caller = SimpleNamespace(session=session, request_id="r1", credential_kind="bearer")
    seen: dict[str, Any] = {}

    async def authenticate(_request: Any) -> Any:
        return caller

    async def invoke(
        op_id: str, params: Any, who: Any, surface: Any, **_kw: Any
    ) -> Any:
        seen["session"] = current_session()
        seen["actor"] = current_actor()
        return {"ok": True}

    op = SimpleNamespace(id="memory.list")
    app = FastAPI()
    app.add_api_route(
        "/ops/memory.list",
        make_endpoint(
            op,
            services=SimpleNamespace(registry=SimpleNamespace(digest="d")),
            authenticate=authenticate,
            invoke=invoke,
            response=lambda outcome, _op, _rid: JSONResponse(outcome),
            generic=True,
        ),
        methods=["POST"],
    )
    assert TestClient(app).post("/ops/memory.list", json={}).json() == {"ok": True}
    assert seen["session"] is session
    assert seen["actor"] is actor
    assert current_session() is None


@pytest.mark.spec("ARBORA-OPS-R007.1")
def test_rest_unauthenticated_names_the_bearer_scheme() -> None:
    from arbora.api.http.auth import HTTPAuthenticationError

    async def authenticate(_request: Any) -> Any:
        raise HTTPAuthenticationError("no bearer")

    async def invoke(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("an unauthenticated request never reaches invoke")

    app = FastAPI()
    app.add_api_route(
        "/ops/memory.list",
        make_endpoint(
            SimpleNamespace(id="memory.list"),
            services=SimpleNamespace(registry=SimpleNamespace(digest="d")),
            authenticate=authenticate,
            invoke=invoke,
            response=lambda outcome, _op, _rid: JSONResponse(outcome),
            generic=True,
        ),
        methods=["POST"],
    )
    reply = TestClient(app).post("/ops/memory.list", json={})
    assert reply.status_code == 401
    assert reply.headers["www-authenticate"] == "Bearer"
