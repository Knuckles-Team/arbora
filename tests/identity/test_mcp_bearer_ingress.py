"""MCP runs ``verify_bearer`` before the engine authority (ARBORA-IDENTITY-R017.5, MCP half)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arbora.core.settings import RuntimeConfig
from arbora.mcp_server.backing.middlewares import ActorContextMiddleware
from arbora.security.bearer import BearerRefusal, BearerRefused
from arbora.security.key_cache import JwksCache


@pytest.mark.spec("ARBORA-IDENTITY-R017.5")
def test_mcp_tool_call_verifies_the_bearer_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    async def precheck(token: str, **_):
        order.append("bearer")
        if token == "bad":
            raise BearerRefused(BearerRefusal.SIGNATURE_INVALID)

    monkeypatch.setattr("arbora.security.bearer.precheck_bearer", precheck)
    middleware = ActorContextMiddleware(
        keys=JwksCache(RuntimeConfig()), require_verified_session=True
    )
    claims = {"sub": "person:ada", "tenant_id": "tenant:one", "scope": "kg:read"}
    monkeypatch.setattr(ActorContextMiddleware, "_claims", lambda self, context: claims)
    monkeypatch.setattr(
        "arbora.security.request_identity.actor_from_claims",
        lambda c: (
            order.append("engine") or (_ for _ in ()).throw(RuntimeError("authority"))
        ),
    )

    async def next_call(context):
        return "ran"

    for token, expected in (("good", RuntimeError), ("bad", PermissionError)):
        order.clear()
        monkeypatch.setattr(
            ActorContextMiddleware, "_raw_token", staticmethod(lambda t=token: t)
        )
        with pytest.raises(expected):
            asyncio.run(middleware.on_call_tool(SimpleNamespace(), next_call))
        assert order[0] == "bearer"
        assert ("engine" in order) == (
            token == "good"
        )  # a refused bearer reaches nothing
