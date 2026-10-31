"""MCP/REST and native HTTP registration for the unary A2A facade."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from .application import AmbientA2AAuthenticator, create_a2a_handlers
from .authority import WorkItemA2AAuthority
from .routing import OrchestratorA2ARouter
from .service import A2AService

if TYPE_CHECKING:
    from arbora.api.http.auth import AmbientHTTPAuthenticator
    from arbora.api.invoke.pipeline import InvokeServices

_SERVICE: A2AService | None = None


def _service() -> A2AService:
    global _SERVICE
    if _SERVICE is None:
        from arbora.mcp_server import runtime

        provider = runtime._get_engine
        _SERVICE = A2AService(
            authority=WorkItemA2AAuthority(provider),
            router=OrchestratorA2ARouter(provider),
        )
    return _SERVICE


def _admitted_handlers(
    operation_services: InvokeServices | None,
    operation_authenticator: AmbientHTTPAuthenticator | None,
) -> tuple[Any, Any]:
    """Validate composition and provider admission before exposing routes."""
    handlers = create_a2a_handlers(
        service=_service(),
        authenticator=AmbientA2AAuthenticator(),
        operation_services=operation_services,
        operation_authenticator=operation_authenticator,
    )
    return handlers


def register_a2a_tools(
    mcp: Any,
    *,
    tools: Any = None,
    operation_services: InvokeServices | None = None,
    operation_authenticator: AmbientHTTPAuthenticator | None = None,
    task_plan_config: Any = None,
    hosted_invocation: Any = None,
) -> None:
    """Register one action router and the protocol-native unary routes.

    The ``graph_a2a`` operation registers on ``tools`` (arbora's backing
    server, reached through ``act``) and the HTTP routes on the served ``mcp``.
    """
    authority = _service().authority
    if isinstance(authority, WorkItemA2AAuthority):
        authority.task_plan_config = task_plan_config
        authority.hosted_invocation = hosted_invocation
    card_handler, rpc_handler = _admitted_handlers(
        operation_services, operation_authenticator
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
        service = _service()
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

    from arbora.mcp_server import runtime

    runtime.REGISTERED_TOOLS["graph_a2a"] = graph_a2a
    mcp.custom_route("/.well-known/agent-card.json", methods=["GET"])(card_handler)
    # A2A clients read the card next to the endpoint the card names.
    mcp.custom_route("/a2a/.well-known/agent-card.json", methods=["GET"])(card_handler)
    mcp.custom_route("/a2a", methods=["POST"])(rpc_handler)


__all__ = ["register_a2a_tools"]
