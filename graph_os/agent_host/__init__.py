"""First-party connector agent host (GRAPHOS-HOST-R009, AU-BOUNDARY-R015).

A fleet connector's ``agent_server.py`` previously called
``agent_utilities.create_agent_server(...)`` to start a standalone HTTP/A2A
listener for its pydantic-graph agent -- the last ``agent_utilities`` import
in about 60 connectors. This module gives GraphOS the same call signature so
a connector can depend on GraphOS (already downstream of agent-utilities in
the composition order) instead of importing agent-utilities directly.

Scope of this first increment (the AU-BOUNDARY-R015 pilot slice): GraphOS
owns the HTTP shell -- health, agent-card discovery, and a bounded A2A
``message/send`` exchange. Agent/model construction stays with
agent-utilities' public API (``agent_utilities.create_graph_agent`` /
``initialize_graph_from_workspace`` and ``agent_utilities.graph.execute_graph``),
which remains the agent orchestration authority per both repositories'
``AGENTS.md``. Deliberately NOT ported in this increment, matching
GRAPHOS-HOST-R009's own scope cut:

- the EG-backed broker/durable task store AU's ``protocols/a2a_epistemic``
  implements (a second task store is not created here either -- a task is a
  single bounded run, not a durable resource);
- the Agent Client Protocol adapter (stays with the agent runtime's harness
  integration per GRAPHOS-HOST-R009);
- the multi-worker gateway fork/spawn pool and the terminal UI launcher.

See ``plans/refactor/reconciliation-20261006/FLEET-SDK-MIGRATION-RECIPE.md``
for the connector migration recipe and the follow-up work this first slice
leaves open.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from graph_os.agent_host.log_access import HostLogAccess
from graph_os.agent_host.log_routes import register_host_log_routes
from graph_os.api.errors import not_served

logger = logging.getLogger(__name__)

__all__ = ["build_agent_host_app"]


#: ``(graph, graph_config, query) -> result dict`` -- the default calls the
#: agent-utilities protocol-agnostic execution port; tests inject a stub so
#: they do not require a live model or MCP transport.
AgentExecutor = Callable[[Any, dict, str], dict]


def _default_executor(graph: Any, graph_config: dict, query: str) -> dict:
    """Run one query through agent-utilities' public execution port."""
    raise not_served("graph", "AU-harness (deleted from AU)", owner="agent-utilities")


def _extract_text(message: dict[str, Any]) -> str:
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    return "".join(
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict) and part.get("kind", "text") == "text"
    )


def _result_text(result: dict[str, Any]) -> str:
    for key in ("output", "content", "result", "response"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class _A2ARequest(BaseModel):
    """JSON-RPC envelope for the bounded ``/a2a`` exchange.

    Defined at module scope (not nested in :func:`build_agent_host_app`) so
    FastAPI's request-model resolution -- which, under
    ``from __future__ import annotations``, resolves a parameter's string
    annotation against the enclosing function's *module* globals -- can find
    it; a class nested inside the builder function is invisible to that
    lookup and silently falls back to treating the parameter as a query
    param instead of a JSON body.
    """

    jsonrpc: str = "2.0"
    method: str
    params: dict[str, Any] | None = None
    id: int | str | None = None


def build_agent_host_app(
    *,
    graph: Any,
    graph_config: dict,
    name: str | None = None,
    description: str | None = None,
    executor: AgentExecutor | None = None,
    host_logs: HostLogAccess | None = None,
):
    """Build the FastAPI app that hosts one connector agent.

    ``graph``/``graph_config`` are the bundle ``agent_utilities.create_graph_agent``
    or ``agent_utilities.initialize_graph_from_workspace`` already returns --
    callers keep using those AU entry points for construction; only the HTTP
    hosting moves here.
    """
    from fastapi import FastAPI

    run_executor = executor or _default_executor
    agent_name = name or "Agent"
    agent_description = description or ""

    app = FastAPI(title=agent_name)
    register_host_log_routes(app, host_logs)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/.well-known/agent-card.json")
    def agent_card() -> dict[str, Any]:
        return {
            "name": agent_name,
            "description": agent_description,
            "protocolVersion": "0.3.0",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "skills": [],
        }

    @app.post("/a2a")
    def a2a_rpc(request: _A2ARequest) -> dict[str, Any]:
        if request.method != "message/send":
            # No durable task store is created here (GRAPHOS-HOST-R009 does
            # not port AU's second broker), so there is nothing for
            # ``tasks/get``/``tasks/cancel`` to look up yet.
            return {
                "jsonrpc": "2.0",
                "id": request.id,
                "error": {"code": -32601, "message": "method not found"},
            }

        params = request.params or {}
        message = params.get("message")
        if not isinstance(message, dict):
            return {
                "jsonrpc": "2.0",
                "id": request.id,
                "error": {"code": -32602, "message": "invalid params"},
            }

        query = _extract_text(message)
        try:
            result = run_executor(graph, graph_config, query)
        except Exception as exc:  # noqa: BLE001 - A2A boundary is fail-closed
            logger.warning(
                "agent_host: execution failed (exception_type=%s)",
                type(exc).__name__,
            )
            return {
                "jsonrpc": "2.0",
                "id": request.id,
                "error": {"code": -32000, "message": "agent execution failed"},
            }

        reply_text = _result_text(result)
        task_id = str(uuid.uuid4())
        return {
            "jsonrpc": "2.0",
            "id": request.id,
            "result": {
                "id": task_id,
                "kind": "task",
                "status": {"state": "completed"},
                "history": [
                    message,
                    {
                        "kind": "message",
                        "role": "agent",
                        "parts": [{"kind": "text", "text": reply_text}],
                        "messageId": str(uuid.uuid4()),
                    },
                ],
            },
        }

    return app
