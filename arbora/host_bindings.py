"""Startup bindings that connect the served host to its ports.

The composition root calls these once while it builds the server:

* :func:`bind_messaging_ports` binds the messaging host ports. The turn
  handler runs one turn on the fleet agent, the action gate is the Arbora
  admission decision for one outbound send, the outbound mirror records a sent
  message in the knowledge engine, and the runtime signal goes to the bounded
  signal buffer.
* :func:`mount_sdk_a2a` mounts the SDK A2A application for the fleet agent
  behind Arbora admission.
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

from arbora.core.settings import setting
from arbora.unique_records import create_record, has_record_writer

__all__ = [
    "A2A_PATH",
    "FLEET_AGENT_NAME",
    "FLEET_TOOLS",
    "SendDecision",
    "action_gate",
    "bind_health_probes",
    "bind_messaging_ports",
    "bind_run_ports",
    "fleet_agent",
    "mount_sdk_a2a",
    "outbound_mirror",
    "turn_handler",
]

#: The intent tools through which the fleet agent reaches the multiplexer.
FLEET_TOOLS = ("find", "act")
#: The fleet's default agent: an A2A message or ``agents.run`` that names no
#: ``metadata.agent_name`` runs on it.
FLEET_AGENT_NAME = "arbora"
#: The default profile requested by agent-webui's orchestrator model.
WEBUI_AGENT_NAME = "webui-assistant"


def preloaded_agent_tools(agent_name: str) -> tuple[str, ...] | None:
    """Select hosted intent tools; caller/session admission remains unchanged."""
    return FLEET_TOOLS if agent_name in {FLEET_AGENT_NAME, WEBUI_AGENT_NAME} else None


#: The model of the fleet agent, as a pydantic-ai model name.
MODEL_SETTING = "ARBORA_AGENT_MODEL"
#: The scopes that admit an outbound message send; each is an engine scope.
SEND_SCOPES = frozenset({"kg:write", "kg:admin"})
#: Where the SDK A2A application is mounted.
A2A_PATH = "/a2a/agent"

_agent: Any = None


def _fleet_toolset(_ctx: Any) -> Any:
    from arbora.mcp_server import runtime

    return runtime.build_native_arbora_toolset(
        list(FLEET_TOOLS), toolset_id="arbora-fleet"
    )


def fleet_agent() -> Any:
    """Return the one fleet agent; its toolset is the multiplexer."""
    global _agent
    if _agent is None:
        from pydantic_ai import Agent

        model = str(setting(MODEL_SETTING, "") or "") or None
        _agent = Agent(model, name=FLEET_AGENT_NAME, toolsets=[_fleet_toolset])
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
    from arbora.api.contracts.session import current_session

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
    if not has_record_writer(engine):
        return
    platform = str(getattr(message, "platform", ""))
    channel = str(getattr(message, "channel_id", ""))
    content = str(getattr(message, "content", ""))
    digest = hashlib.sha256(
        f"{platform}\x00{channel}\x00{content}".encode()
    ).hexdigest()
    await asyncio.to_thread(
        create_record,
        engine,
        f"outbound:{digest[:32]}",
        "OutboundMessage",
        {
            "platform": platform,
            "channel_id": channel,
            "text": content,
            "sent_at": time.time(),
        },
    )


def bind_messaging_ports() -> None:
    """Bind the four messaging host ports at startup."""
    from arbora.messaging.host_ports import bind_host_ports
    from arbora.observability.runtime_signals import record_runtime_signal

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
        # Route registries key a mounted endpoint by its name.
        self.__name__ = "a2a_agent"

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = str(scope.get("path", ""))
        inner = path[len(self.prefix) :] if path.startswith(self.prefix) else path
        await self.app(
            {**scope, "path": inner or "/", "root_path": self.prefix}, receive, send
        )


class A2AAppExtension(ServerExtension):
    """Run the A2A application's task manager and worker for the server's life."""

    identifier = "arbora/sdk-a2a"

    def __init__(self, app: Any) -> None:
        self._app = app

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        # FastMCP shields extension teardown in its task. Keep the SDK's AnyIO
        # scopes in their own owner task so they exit in the scope that entered
        # them, while TaskGroup propagates failures and joins every shutdown.
        ready = asyncio.Event()
        stop = asyncio.Event()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._run_lifespan(ready, stop))
            await ready.wait()
            try:
                yield
            finally:
                stop.set()

    async def _run_lifespan(self, ready: asyncio.Event, stop: asyncio.Event) -> None:
        owner = asyncio.create_task(self._own_lifespan(ready, stop))
        try:
            await asyncio.shield(owner)
        except asyncio.CancelledError:
            stop.set()
            if not ready.is_set():
                owner.cancel()
            # Once started, cancellation requests orderly SDK shutdown rather
            # than interrupting its cleanup awaits. Always join the owner.
            await owner
            raise

    async def _own_lifespan(self, ready: asyncio.Event, stop: asyncio.Event) -> None:
        async with self._app.router.lifespan_context(self._app):
            ready.set()
            await stop.wait()


def mount_sdk_a2a(mcp: Any) -> Any:
    """Mount the SDK A2A application for the fleet agent behind admission."""
    from fasta2a.broker import InMemoryBroker
    from fasta2a.storage import InMemoryStorage

    from arbora import __version__
    from arbora.a2a.application import (
        AmbientA2AAuthenticator,
        _caller_session_scope,
    )
    from arbora.a2a.sdk_app import create_admitted_a2a_app

    base = str(setting("ARBORA_PUBLIC_URL", "") or "").rstrip("/")
    admitted = create_admitted_a2a_app(
        agent=fleet_agent(),
        authenticator=AmbientA2AAuthenticator(),
        bind_caller=_caller_session_scope,
        name="arbora",
        url=f"{base}{A2A_PATH}",
        version=__version__,
        storage=InMemoryStorage(),
        broker=InMemoryBroker(),
        description="The Arbora fleet agent over A2A.",
    )
    served = _PrefixedApp(admitted, A2A_PATH)
    mcp.custom_route(A2A_PATH, methods=["POST"])(served)
    mcp.custom_route(f"{A2A_PATH}/.well-known/agent-card.json", methods=["GET"])(served)
    mcp.add_extension(A2AAppExtension(admitted.app))
    return admitted


#: The engine's Cypher dialect requires a MATCH clause: a bare ``RETURN 1`` is
#: refused (INVALID_ARGUMENT), which held every served pod's readiness at 503.
ENGINE_PROBE_QUERY = "MATCH (n) RETURN n LIMIT 1"


def _engine_probe(session: Any) -> dict[str, Any]:
    from arbora.mcp_server import runtime

    engine = runtime._get_engine()
    if engine is None:
        return {"status": "unhealthy", "reason": "no engine is bound"}
    from arbora.api.contracts.session import use_session

    if session is None:
        engine.query_cypher(ENGINE_PROBE_QUERY, {})
    else:
        with use_session(session):
            engine.query_cypher(ENGINE_PROBE_QUERY, {})
    return {"status": "ok"}


def _fleet_probe(session: Any) -> dict[str, Any]:
    from arbora.api.contracts.session import use_session
    from arbora.fleet.health import collect_fleet_health
    from arbora.gateway import fleet as fleet_api

    def _collect() -> Any:
        return collect_fleet_health(
            fleet_api._fleet_authorities(), scope_resolver=fleet_api._tenant_scope
        )

    if session is None:
        snapshot = _collect()
    else:
        with use_session(session):
            snapshot = _collect()
    detail = snapshot.evidence.model_dump(mode="json")
    if snapshot.evidence.ready:
        return {"status": "ok", "detail": detail}
    return {
        "status": "unhealthy",
        "reason": f"fleet supervisory evidence is {snapshot.evidence.status}",
        "detail": detail,
    }


def bind_run_ports() -> None:
    """Bind the harness run-loop decision, model and event providers."""
    from arbora.agent_runs import bind_run_ports as _bind

    _bind()


def bind_health_probes() -> None:
    """Bind the mandatory readiness probes."""
    from arbora.observability.runtime_health import register_health_probe

    register_health_probe("engine", _engine_probe)
    register_health_probe("fleet_supervision", _fleet_probe)
