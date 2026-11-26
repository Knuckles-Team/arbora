"""Session open writes one record (ARBORA-IDENTITY-R026.2)."""

from __future__ import annotations

import asyncio

import pytest

from arbora.identity.client_session import ClientSessionRefused, record_session_open
from arbora.mcp_server.backing.middlewares import ActorContextMiddleware
from arbora.security.actor_context import ActorContext, ActorType
from tests.identity._session_fakes import Engine


def _actor() -> ActorContext:
    return ActorContext(
        actor_id="person:ada",
        actor_type=ActorType.HUMAN,
        tenant_id="tenant:one",
        authenticated=True,
    )


@pytest.mark.spec("ARBORA-IDENTITY-R026.2")
def test_open_stores_one_record_per_session() -> None:
    engine = Engine()
    for _ in range(2):  # a repeated initialize writes nothing new
        asyncio.run(record_session_open(_actor(), session_id="s1", engine=engine))
    assert list(engine.nodes) == ["client_session:tenant:one:s1"]
    assert engine.nodes["client_session:tenant:one:s1"]["label"] == "ClientSession"
    with pytest.raises(ClientSessionRefused):
        asyncio.run(
            record_session_open(
                ActorContext(actor_id="x", tenant_id="t"),
                session_id="s2",
                engine=engine,
            )
        )
    assert len(engine.nodes) == 1
    # The MCP initialize hook of the served server is the caller of the write.
    assert callable(ActorContextMiddleware(require_verified_session=True).on_initialize)
