"""Shared harness for served-operation tests with a recording engine.

The ``served`` fixture of ``tests/test_served_operation_api.py`` composes the
real invocation; this module captures it, routes named engine methods to a
recording handler, and lets a test choose which verified person calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastmcp import FastMCP

from arbora.mcp_server import runtime as mcp_runtime
from arbora.security import request_identity
from arbora.security.actor_context import ActorContext, ActorType
from tests.test_served_operation_api import SCOPES

TENANT = "tenant:one"


def person(name: str, tenant: str = TENANT) -> ActorContext:
    return ActorContext(
        actor_id=f"person:{name}",
        actor_type=ActorType.HUMAN,
        tenant_id=tenant,
        authenticated=True,
        roles=SCOPES,
        credential_expires_at=int(time.time()) + 3600,
    )


def capture_invocation(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the invocation the ``served`` fixture composes (request it first)."""
    import tests.test_served_operation_api as harness

    captured: dict[str, Any] = {}
    compose = harness.compose_served_invocation

    def capturing(**kwargs: Any) -> Any:
        captured["invocation"] = compose(**kwargs)
        return captured["invocation"]

    monkeypatch.setattr(harness, "compose_served_invocation", capturing)
    return captured


def route_methods(
    client: Any,
    handlers: dict[str, Callable[[dict[str, Any], str], Any]],
    monkeypatch: pytest.MonkeyPatch,
    people: dict[str, ActorContext],
) -> None:
    """Send ``handlers``' methods to a recording engine as the verified person.

    ``people`` maps a bearer credential to the person it verifies as. The
    handler receives the wire params and that person's principal id.
    """
    import medulla.generated as generated
    from medulla.generated import _runtime

    # The pinned engine release predates these methods; declare them for the test.
    declared = frozenset(generated.METHOD_IDS) | frozenset(handlers)
    monkeypatch.setattr(generated, "METHOD_IDS", declared)
    monkeypatch.setattr(_runtime, "METHOD_IDS", declared)
    original = client._send
    current: dict[str, str] = {"principal": "person:one"}

    async def credential_owner(credential: str) -> ActorContext:
        actor = people[credential]
        current["principal"] = actor.actor_id
        return actor

    monkeypatch.setattr(request_identity, "actor_from_bearer_token", credential_owner)

    async def send(method: str, params: Any, graph: str | None, **kw: Any) -> Any:
        if method in handlers:
            return handlers[method](params, current["principal"])
        return await original(method, params, graph, **kw)

    client._send = send


class McpAsk:
    """The served MCP intent surface over the captured invocation."""

    def __init__(self, invocation: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        process = request_identity.mint_graph_session(person("one"))
        monkeypatch.setattr(mcp_runtime, "_PROCESS_SESSION", process)
        self.mcp = FastMCP("served-ops")
        mcp_runtime._register_served_intent_surface(self.mcp, invocation)

    def __call__(self, verb: str, op: str, params: dict[str, Any], **extra: Any) -> Any:
        async def call() -> Any:
            result = await self.mcp.call_tool(
                verb, {"op": op, "params": params, **extra}
            )
            text = "".join(getattr(part, "text", "") for part in result.content)
            return json.loads(text)

        return asyncio.run(call())


@contextlib.contextmanager
def nothing() -> Iterator[None]:
    yield
