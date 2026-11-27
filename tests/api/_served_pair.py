"""One operation served over HTTP and over MCP through the real ``invoke`` pipeline.

The registry holds only the operation under test; the dispatch step is replaced by the
handler under test, so both transports run admission, scope checks, screening, audit and
replay exactly as production does and reach the same handler with a real verified caller.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import httpx

from arbora.api.http.app import create_api_application
from arbora.api.http.auth import AmbientHTTPAuthenticator
from arbora.api.invoke.pipeline import invoke
from arbora.api.mcp.caller import caller_from_session
from arbora.api.mcp.verbs import MCPProjection, dispatch_verb
from arbora.api.registry import Registry
from tests.a2a.test_operation_bridge import session
from tests.api.test_invoke_contract import setup_services

Handler = Callable[[Any, Any, Any], Awaitable[Any]]


async def serve_both(
    op: Any,
    handler: Handler,
    params: dict[str, Any],
    *,
    scopes: frozenset[str],
    verb: str = "act",
    headers: dict[str, str] | None = None,
) -> tuple[httpx.Response, Any, Any]:
    """Return the HTTP response, the MCP result and the verified caller used."""
    services, runtime, *_ = setup_services(op, registry=Registry([op]))
    caller = caller_from_session(session(scopes), credential_kind="bearer")

    async def dispatch(operation: Any, arguments: Any, execution: Any) -> Any:
        return await handler(operation, arguments, execution)

    async def authenticate(request: Any) -> Any:
        return caller

    async def visible(operation: Any, verified: Any) -> bool:
        return True

    runtime.dispatch = dispatch
    auth = AmbientHTTPAuthenticator(session_for_request=lambda _: caller.session)
    auth.authenticate = authenticate
    app = create_api_application(
        services=services, authenticator=auth, invoke=invoke, visibility=visible
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.post(
            f"/ops/{op.id}", json=params, headers=headers or {}
        )
    projection = MCPProjection(
        services.registry,
        services,
        SimpleNamespace(scope_ref=lambda *args, **kwargs: "scope"),
        lambda: caller,
        services.policy_gate,
        invoke,
    )
    mcp = await dispatch_verb(verb, projection, op=op.id, params=params)
    return response, mcp, caller
