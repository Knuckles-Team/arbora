"""MCP/REST and native HTTP registration for the unary A2A facade."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any, Literal

from arbora.security.key_cache import JwksCache

from .application import AmbientA2AAuthenticator, create_a2a_handlers
from .authority import WorkItemA2AAuthority
from .service import A2AService

if TYPE_CHECKING:
    from arbora.api.http.auth import AmbientHTTPAuthenticator
    from arbora.api.invoke.pipeline import InvokeServices


def _admitted_handlers(
    keys: JwksCache,
    service: A2AService,
    operation_services: InvokeServices | None,
    operation_authenticator: AmbientHTTPAuthenticator | None,
    approvals: Any = None,
) -> tuple[Any, Any]:
    """Validate composition and provider admission before exposing routes."""
    handlers = create_a2a_handlers(
        service=service,
        authenticator=AmbientA2AAuthenticator(keys),
        operation_services=operation_services,
        operation_authenticator=operation_authenticator,
        approvals=approvals,
    )
    return handlers


def register_a2a_tools(
    mcp: Any,
    *,
    service: A2AService,
    tools: Any = None,
    tool_table: MutableMapping[str, Any],
    operation_services: InvokeServices | None = None,
    operation_authenticator: AmbientHTTPAuthenticator | None = None,
    task_plan_config: Any = None,
    hosted_invocation: Any = None,
    approval_chain: Any = None,
    anonymous: Any = None,
    keys: JwksCache,
) -> Any:
    """Register one action router and the protocol-native routes.

    Returns the shared JSON-RPC handler, which also serves the streams; the SDK
    route serves its streams through it (ARBORA-A2A-R001.4.1).

    The ``graph_a2a`` operation registers on ``tools`` (arbora's backing
    server, reached through ``act``) and the HTTP routes on the served ``mcp``.

    ``anonymous`` is the provider of the none-mode anonymous task application: in none
    mode ``POST /a2a`` is answered by it when it composes (ARBORA-IDENTITY-R004.8.2).
    """
    authority = service.authority
    if isinstance(authority, WorkItemA2AAuthority):
        authority.task_plan_config = task_plan_config
        authority.hosted_invocation = hosted_invocation
    card_handler, rpc_handler = _admitted_handlers(
        keys,
        service,
        operation_services,
        operation_authenticator,
        approval_chain,
    )
    registrar = tools if tools is not None else mcp

    @registrar.tool(
        name="graph_a2a",
        description=(
            "Discover or manage governed unary A2A tasks. Actions: card, send, "
            "get, list, cancel. Streaming and push are not supported."
        ),
        tags={"arbora", "a2a", "orchestration"},
    )
    async def graph_a2a(
        action: Literal["card", "send", "get", "list", "cancel"],
        message: dict[str, Any] | None = None,
        task_id: str = "",
        idempotency_key: str = "",
        cursor: str = "",
        limit: int = 50,
        context_budget_tokens: int = 0,
        endpoint_url: str = "/a2a",
        include_committed_event: bool = False,
    ) -> str:
        if action == "card":
            result: Any = service.agent_card(endpoint_url)
        elif action == "send":
            if message is None:
                raise ValueError("message is required")
            result = await service.send_message(
                message=message,
                idempotency_key=idempotency_key,
                context_budget_tokens=context_budget_tokens or None,
            )
        elif action == "get":
            result = await service.get_task(
                task_id, include_committed_event=include_committed_event
            )
            if result is None:
                raise ValueError("A2A task not found")
        elif action == "list":
            result = await service.list_tasks(cursor=cursor or None, limit=limit)
        else:
            result = await service.cancel_task(task_id)
        return result.model_dump_json(by_alias=True)

    tool_table["graph_a2a"] = graph_a2a
    mcp.custom_route("/.well-known/agent-card.json", methods=["GET"])(card_handler)
    # A2A clients read the card next to the endpoint the card names.
    mcp.custom_route("/a2a/.well-known/agent-card.json", methods=["GET"])(card_handler)
    from starlette.routing import request_response

    from .anonymous_host import AnonymousA2AEntry

    mcp.custom_route("/a2a", methods=["POST"])(
        AnonymousA2AEntry(request_response(rpc_handler), anonymous, "/a2a")
    )
    return rpc_handler


__all__ = ["register_a2a_tools"]
