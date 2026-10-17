from __future__ import annotations

"""MCP Context Helper Utilities.

Standardized helpers for the FastMCP ``Context`` object. Every MCP server
in the agent-packages ecosystem should import these instead of hand-rolling
ctx interactions. Read-only helpers are no-ops when *ctx* is ``None``;
destructive confirmation fails closed.

CONCEPT:AU-ECO.mcp.standardized-interfaces — MCP Standardized Interfaces
"""


import json
import logging
from typing import Any

from graph_os.security.error_surface import public_error_payload

logger = logging.getLogger(__name__)


async def ctx_progress(ctx: Any, progress: int, total: int = 100) -> None:
    """Report progress to the MCP client if a context is available.

    Args:
        ctx: FastMCP ``Context`` (may be ``None``).
        progress: Current progress value.
        total: Total steps (default 100 for percentage-style reporting).

    """
    if ctx:
        await ctx.report_progress(progress=progress, total=total)


async def ctx_confirm_destructive(
    ctx: Any,
    action_description: str,
) -> bool:
    """Standard elicitation guard for destructive operations.

    When a ``Context`` is available this asks the human user to confirm
    before proceeding. Missing context and elicitation failures deny the
    operation because unattended execution is not write authorization.

    Args:
        ctx: FastMCP ``Context`` (may be ``None``).
        action_description: Human-readable description of the action,
            e.g. ``"delete stack 'production'"``.

    Returns:
        ``True`` if the operation should proceed, ``False`` if cancelled.

    """
    if not ctx:
        return False
    try:
        result = await ctx.elicit(
            f"⚠️ Are you sure you want to {action_description}?",
            response_type=bool,
        )
        return result.action == "accept" and bool(result.data)
    except Exception as exc:
        logger.warning(
            "Elicitation failed; destructive operation denied (exception_type=%s)",
            type(exc).__name__,
        )
        return False


def ctx_log(
    ctx: Any,
    server_logger: logging.Logger,
    level: str,
    message: str,
) -> None:
    """Dual-log a message to *both* the server-side logger and the MCP client.

    This ensures that diagnostic output is visible in two places:
    • The server process logs (for operators / container logs).
    • The MCP client log console (for the SI agent / human user).

    Args:
        ctx: FastMCP ``Context`` (may be ``None``).
        server_logger: A standard Python :class:`logging.Logger`.
        level: Log level string — ``"debug"``, ``"info"``, ``"warning"``,
            or ``"error"``.
        message: The log message.

    """
    getattr(server_logger, level, server_logger.info)(message)
    if ctx:
        client_fn = getattr(ctx, level, None) or getattr(ctx, "info", None)
        if client_fn:
            try:
                import asyncio
                import inspect

                res = client_fn(message)
                if inspect.iscoroutine(res):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(res)
                    except RuntimeError:
                        pass
            except Exception:
                pass  # nosec


async def ctx_set_state(
    ctx: Any,
    project: str,
    key: str,
    value: Any,
) -> None:
    """Store a value in the MCP session state with a standardized key.

    Keys are namespaced as ``"{project}_{key}"`` to prevent collisions
    across different MCP servers sharing the same session.

    Args:
        ctx: FastMCP ``Context`` (may be ``None``).
        project: Project namespace (e.g. ``"portainer"``, ``"gitlab"``).
        key: State key (e.g. ``"auth_token"``, ``"active_context"``).
        value: The value to store.

    """
    if ctx and hasattr(ctx, "session"):
        try:
            namespaced = f"{project}_{key}"
            await ctx.session.set_state(namespaced, value)
        except Exception as exc:
            logger.debug(
                "ctx_set_state failed (exception_type=%s)",
                type(exc).__name__,
            )


async def ctx_get_state(
    ctx: Any,
    project: str,
    key: str,
    default: Any = None,
) -> Any:
    """Retrieve a value from the MCP session state.

    Args:
        ctx: FastMCP ``Context`` (may be ``None``).
        project: Project namespace.
        key: State key.
        default: Fallback if the key is missing or ctx is unavailable.

    Returns:
        The stored value, or *default*.

    """
    if ctx and hasattr(ctx, "session"):
        try:
            namespaced = f"{project}_{key}"
            val = await ctx.session.get_state(namespaced)
            return val if val is not None else default
        except Exception:
            pass  # nosec
    return default


async def ctx_sample(
    ctx: Any,
    prompt: str,
    system_prompt: str | None = None,
) -> str | None:
    """Ask the client LLM to generate a response (sampling).

    This is an optional capability — it only works when the connected MCP
    client supports the ``sampling`` feature.  Returns ``None`` silently
    when sampling is unavailable.

    Args:
        ctx: FastMCP ``Context`` (may be ``None``).
        prompt: The user-turn prompt to send to the LLM.
        system_prompt: Optional system prompt to guide the LLM.

    Returns:
        The LLM-generated text, or ``None`` if sampling is unavailable.

    """
    if not ctx:
        return None
    try:
        from mcp.types import SamplingMessage, TextContent

        messages = [
            SamplingMessage(
                role="user",
                content=TextContent(type="text", text=prompt),
            )
        ]
        result = await ctx.sample(
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=2048,
        )
        if result and hasattr(result, "content"):
            return (
                result.content.text
                if hasattr(result.content, "text")
                else str(result.content)
            )
        return None
    except ImportError:
        logger.debug("mcp.types not available — sampling disabled.")
        return None
    except Exception as exc:
        logger.debug("ctx_sample failed (exception_type=%s)", type(exc).__name__)
        return None


async def ctx_graphql_list_types(
    execute_gql_fn: Any,
) -> dict[str, Any]:
    """Discover all types in a GraphQL schema using standard introspection.

    Args:
        execute_gql_fn: A callable (sync or async) that executes a GraphQL query
            string and returns a dict response.

    Returns:
        A dictionary containing type names and descriptions.
    """
    query = """
    query IntrospectionQuery {
      __schema {
        types {
          name
          kind
          description
        }
      }
    }
    """
    import inspect

    try:
        if inspect.iscoroutinefunction(execute_gql_fn):
            res = await execute_gql_fn(query)
        else:
            res = execute_gql_fn(query)
        return res
    except Exception as exc:
        return public_error_payload(exc, logger=logger)


async def ctx_graphql_get_type_details(
    execute_gql_fn: Any,
    type_name: str,
) -> dict[str, Any]:
    """Retrieve detailed field information for a specific type in a GraphQL schema.

    Args:
        execute_gql_fn: A callable (sync or async) that executes a GraphQL query
            string and returns a dict response.
        type_name: Name of the type to inspect.

    Returns:
        A dictionary containing the detailed fields, arguments, and types of the type.
    """
    query = """
    query GetTypeDetails($name: String!) {
      __type(name: $name) {
        name
        kind
        description
        fields {
          name
          description
          args {
            name
            description
            type {
              name
              kind
              ofType {
                name
                kind
              }
            }
          }
          type {
            name
            kind
            ofType {
              name
              kind
            }
          }
        }
      }
    }
    """
    import inspect

    try:
        variables = {"name": type_name}
        if inspect.iscoroutinefunction(execute_gql_fn):
            res = await execute_gql_fn(query, variables=variables)
        else:
            res = execute_gql_fn(query, variables=variables)
        return res
    except Exception as exc:
        return public_error_payload(exc, logger=logger)


def coerce_dict_param(v: Any) -> dict:
    """A dict tool parameter as a plain dict.

    Omitted dict params arrive as the unresolved FastMCP ``FieldInfo``
    (default_factory is not resolved by the internal/REST dispatcher); coerce
    anything non-dict, and a JSON string some clients send, to a dict.
    """
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def run_coro(coro: Any) -> Any:
    """Run an async coroutine from a sync MCP handler, loop-running or not.

    With no running loop it uses ``asyncio.run``; with one, a worker thread
    with its own loop, so a running loop is never re-entered.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


def caller_identity_fields(caller: Any) -> dict[str, Any]:
    """A Tasks delegation caller's tenant, owner and sorted scopes."""
    return {
        "tenant": str(caller.get("tenant") or ""),
        "owner": str(caller.get("owner") or ""),
        "scopes": sorted(str(scope) for scope in caller.get("scopes", ())),
    }


def token_caller_material(token: Any) -> str:
    """The NUL-joined client id, subject and tenant one access token names."""
    claims = getattr(token, "claims", None) or {}
    return "\x00".join(
        str(value or "")
        for value in (
            getattr(token, "client_id", None),
            claims.get("sub") if isinstance(claims, dict) else None,
            claims.get("tenant_id") if isinstance(claims, dict) else None,
        )
    )


def action_policy_gate(
    kind: str, target: str, reason: str, actor_id: str | None = None
) -> tuple[bool, dict]:
    """Run the fail-closed ActionPolicy gate for one mutation.

    A thin call into the shared ``get_action_policy(engine).decide(...)``
    decision point on the MCP-layer engine, never a reimplementation of its
    rules. Returns ``(allowed, decision info)``.
    """
    from graph_os.access.action_policy import (
        ActionRequest,
        get_action_policy,
    )
    from graph_os.mcp_server import runtime

    request: dict[str, Any] = {
        "kind": kind,
        "target": target,
        "source": "mcp",
        "reason": reason or f"{kind} on {target}",
    }
    if actor_id is not None:
        request["actor_id"] = actor_id
    decision = get_action_policy(runtime._get_engine()).decide(ActionRequest(**request))
    info = {
        "decision": decision.decision,
        "tier": decision.tier,
        "reason": decision.reason,
        "approval_id": decision.approval_id,
        "audit_id": decision.audit_id,
    }
    return decision.allowed, info
