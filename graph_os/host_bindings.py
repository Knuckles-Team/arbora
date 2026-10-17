"""Startup bindings that connect the served host to its ports.

The composition root calls these once while it builds the server:

* :func:`bind_messaging_ports` binds the messaging host ports. The turn
  handler runs one turn on the fleet agent, the action gate is the Graph OS
  admission decision for one outbound send, the outbound mirror records a sent
  message in the knowledge engine, and the runtime signal goes to the bounded
  signal buffer.
* :func:`mount_sdk_a2a` mounts the SDK A2A application for the fleet agent
  behind Graph OS admission.
* :func:`bind_health_probes` binds the readiness probes for the engine and for
  fleet supervision.

The fleet agent reaches the multiplexer through the ``find`` and ``act``
intent tools, and every call dispatches through the verified tool path.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastmcp.server.extensions import ServerExtension

from graph_os.core.settings import setting
from graph_os.security.scope_registry import SCOPE_CLASSES

__all__ = [
    "A2A_PATH",
    "FLEET_TOOLS",
    "SendDecision",
    "action_gate",
    "bind_health_probes",
    "bind_messaging_ports",
    "fleet_agent",
    "mount_sdk_a2a",
    "outbound_mirror",
    "turn_handler",
]

#: The intent tools through which the fleet agent reaches the multiplexer.
FLEET_TOOLS = ("find", "act")
#: The model of the fleet agent, as a pydantic-ai model name.
MODEL_SETTING = "GRAPH_OS_AGENT_MODEL"
#: The scopes that admit an outbound message send; each is an engine scope.
SEND_SCOPES = frozenset({"kg:write", "kg:admin"})
if not SEND_SCOPES <= SCOPE_CLASSES.keys():
    raise RuntimeError("a send scope is not in the engine scope registry")
#: Where the SDK A2A application is mounted.
A2A_PATH = "/a2a/agent"

_agent: Any = None


def _fleet_toolset(_ctx: Any) -> Any:
    from graph_os.mcp_server import runtime

    return runtime.build_native_graphos_toolset(
        list(FLEET_TOOLS), toolset_id="graphos-fleet"
    )


def fleet_agent() -> Any:
    """Return the one fleet agent; its toolset is the multiplexer."""
    global _agent
    if _agent is None:
        from pydantic_ai import Agent

        model = str(setting(MODEL_SETTING, "") or "") or None
        _agent = Agent(model, name="graph-os", toolsets=[_fleet_toolset])
    return _agent


async def turn_handler(engine: Any, text: str, *, session: Any = "") -> str | None:
    """Run one inbound turn on the fleet agent and return the reply text."""
    del engine, session
    result = await fleet_agent().run(text)
    output = getattr(result, "output", None)
    return str(output) if output else None


@dataclass(frozen=True, slots=True)
class SendDecision:
    """The admission decision for one outbound send."""

    allowed: bool
    decision: str
    reason: str


def _current_session() -> Any:
    from graph_os.api.contracts.session import current_session

    return current_session()


def action_gate(request: Mapping[str, str], engine: Any) -> SendDecision:
    """Admit an outbound send only for a verified session with a send scope."""
    del engine
    session = _current_session()
    scopes = frozenset(getattr(session, "scopes", ()) or ())
    if session is None:
        return SendDecision(False, "deny", "no verified session")
    if scopes & SEND_SCOPES:
        return SendDecision(True, "allow", f"{request.get('kind', 'send')} admitted")
    return SendDecision(False, "deny", "the session holds no send scope")


async def outbound_mirror(message: Any, engine: Any) -> None:
    """Record one sent message as an ``OutboundMessage`` node in the engine."""
    add_node = getattr(engine, "add_node", None) if engine is not None else None
    if not callable(add_node):
        return
    platform = str(getattr(message, "platform", ""))
    channel = str(getattr(message, "channel_id", ""))
    content = str(getattr(message, "content", ""))
    digest = hashlib.sha256(
        f"{platform}\x00{channel}\x00{content}".encode()
    ).hexdigest()
    await asyncio.to_thread(
        add_node,
        f"outbound:{digest[:32]}",
        "OutboundMessage",
        properties={
            "platform": platform,
            "channel_id": channel,
            "text": content,
            "sent_at": time.time(),
        },
    )


def bind_messaging_ports() -> None:
    """Bind the four messaging host ports at startup."""
    from graph_os.messaging.host_ports import bind_host_ports
    from graph_os.observability.runtime_signals import record_runtime_signal

    def runtime_signal(kind: str, subject: str, detail: Mapping[str, Any]) -> None:
        record_runtime_signal(kind, subject, dict(detail))

    bind_host_ports(
        turn_handler=turn_handler,
        action_gate=action_gate,
        outbound_mirror=outbound_mirror,
        runtime_signal=runtime_signal,
    )


class _PrefixedApp:
    """Serve an ASGI application under ``prefix`` by stripping it from the path."""

    def __init__(self, app: Any, prefix: str) -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = str(scope.get("path", ""))
        inner = path[len(self.prefix) :] if path.startswith(self.prefix) else path
        await self.app(
            {**scope, "path": inner or "/", "root_path": self.prefix}, receive, send
        )


class A2AAppExtension(ServerExtension):
    """Run the A2A application's task manager and worker for the server's life."""

    identifier = "graph-os/sdk-a2a"

    def __init__(self, app: Any) -> None:
        self._app = app

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        async with self._app.router.lifespan_context(self._app):
            yield


def mount_sdk_a2a(mcp: Any) -> Any:
    """Mount the SDK A2A application for the fleet agent behind admission."""
    from fasta2a.broker import InMemoryBroker
    from fasta2a.storage import InMemoryStorage

    from graph_os import __version__
    from graph_os.a2a.application import (
        AmbientA2AAuthenticator,
        _caller_session_scope,
    )
    from graph_os.a2a.sdk_app import create_admitted_a2a_app

    base = str(setting("GRAPH_OS_PUBLIC_URL", "") or "").rstrip("/")
    admitted = create_admitted_a2a_app(
        agent=fleet_agent(),
        authenticator=AmbientA2AAuthenticator(),
        bind_caller=_caller_session_scope,
        name="graph-os",
        url=f"{base}{A2A_PATH}",
        version=__version__,
        storage=InMemoryStorage(),
        broker=InMemoryBroker(),
        description="The Graph OS fleet agent over A2A.",
    )
    served = _PrefixedApp(admitted, A2A_PATH)
    mcp.custom_route(A2A_PATH, methods=["POST"])(served)
    mcp.custom_route(f"{A2A_PATH}/.well-known/agent-card.json", methods=["GET"])(served)
    mcp.add_extension(A2AAppExtension(admitted.app))
    return admitted


def _engine_probe(session: Any) -> dict[str, Any]:
    from graph_os.mcp_server import runtime

    engine = runtime._get_engine()
    if engine is None:
        return {"status": "unhealthy", "reason": "no engine is bound"}
    from graph_os.api.contracts.session import use_session

    if session is None:
        engine.query_cypher("RETURN 1 AS ok", {})
    else:
        with use_session(session):
            engine.query_cypher("RETURN 1 AS ok", {})
    return {"status": "ok"}


def _fleet_probe(session: Any) -> dict[str, Any]:
    from graph_os.api.contracts.session import use_session
    from graph_os.gateway import fleet as fleet_api

    if session is None:
        snapshot = fleet_api.collect_fleet_health()
    else:
        with use_session(session):
            snapshot = fleet_api.collect_fleet_health()
    detail = snapshot.evidence.model_dump(mode="json")
    if snapshot.evidence.ready:
        return {"status": "ok", "detail": detail}
    return {
        "status": "unhealthy",
        "reason": f"fleet supervisory evidence is {snapshot.evidence.status}",
        "detail": detail,
    }


def bind_health_probes() -> None:
    """Bind the mandatory readiness probes."""
    from graph_os.observability.runtime_health import register_health_probe

    register_health_probe("engine", _engine_probe)
    register_health_probe("fleet_supervision", _fleet_probe)
