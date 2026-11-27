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
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from fastmcp.server.extensions import ServerExtension

from arbora.core.agent_names import FLEET_AGENT_NAME
from arbora.core.environment import read_env
from arbora.observability.state import HealthState, RuntimeSignalBuffer
from arbora.security.key_cache import JwksCache
from arbora.unique_records import create_record, has_record_writer

__all__ = [
    "A2A_PATH",
    "SendDecision",
    "action_gate",
    "bind_health_probes",
    "bind_messaging_ports",
    "fleet_agent",
    "mount_sdk_a2a",
    "outbound_mirror",
    "prompt_normalizer",
    "turn_handler",
]

#: The model of the fleet agent, as a pydantic-ai model name.
MODEL_SETTING = "ARBORA_AGENT_MODEL"
#: The scopes that admit an outbound message send; each is an engine scope.
SEND_SCOPES = frozenset({"kg:write", "kg:admin"})
#: Where the SDK A2A application is mounted.
A2A_PATH = "/a2a/agent"


class HostTools(Protocol):
    """What the host bindings read of the host tool table: its read-only intent verbs."""

    @property
    def verbs(self) -> Mapping[str, Any]: ...


def fleet_agent(build_toolset: Callable[[], Any]) -> Any:
    """Build the fleet agent; ``build_toolset`` is the host's toolset over its tool table."""
    from pydantic_ai import Agent

    def toolset(_ctx: Any) -> Any:
        return build_toolset()

    model = str(read_env(MODEL_SETTING, "") or "") or None
    return Agent(model, name=FLEET_AGENT_NAME, toolsets=[toolset])


async def turn_handler(
    build_toolset: Callable[[], Any], engine: Any, text: str, *, session: Any = ""
) -> str | None:
    """Run one inbound turn on the fleet agent and return the reply text.

    ``session`` is the sender's ``SenderSession`` (ARBORA-MESSAGING-R007.1) for a
    channel turn; the fleet agent runs under the principal it names.
    """
    del engine, session
    result = await fleet_agent(build_toolset).run(text)
    output = getattr(result, "output", None)
    return str(output) if output else None


@dataclass(frozen=True, slots=True)
class SendDecision:
    """The admission decision for one outbound send."""

    allowed: bool
    decision: str
    reason: str


def _current_session() -> Any:
    from arbora.security.session import current_session

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


def prompt_normalizer(
    engine_provider: Callable[[], Any], verbs: Mapping[str, Any]
) -> Any:
    """The process modality seam, recording provenance in the served engine.

    ARBORA-OPS-R899: audio parts go to the fleet's speech-to-text capability
    through the governed ``find``/``act`` verbs; each kept original and its
    transcript link are written as ``MediaAttachment``/``Transcript`` records.
    """
    from arbora.modality import default_normalizer
    from arbora.modality.fleet_port import NativeIntentFleet
    from arbora.modality.provenance import engine_recorder

    return default_normalizer(
        fleet=NativeIntentFleet(verbs), recorder=engine_recorder(engine_provider)
    )


def bind_messaging_ports(
    engine_provider: Callable[[], Any],
    tools: HostTools,
    signals: RuntimeSignalBuffer,
    delegation: Mapping[str, Any],
    *,
    build_toolset: Callable[[], Any],
) -> None:
    """Bind the messaging host ports at startup; the turn handler runs over the toolset."""
    from functools import partial

    from arbora.messaging.host_ports import bind_host_ports
    from arbora.observability.runtime_signals import record_runtime_signal

    def runtime_signal(kind: str, subject: str, detail: Mapping[str, Any]) -> None:
        record_runtime_signal(signals, kind, subject, dict(detail))

    bind_host_ports(
        turn_handler=partial(turn_handler, build_toolset),
        action_gate=action_gate,
        outbound_mirror=outbound_mirror,
        runtime_signal=runtime_signal,
        prompt_normalizer=prompt_normalizer(engine_provider, tools.verbs),
        delegation=delegation,
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


def _engine_storage(engine_provider: Callable[[], Any]) -> Any:
    """The served agent's task and conversation storage: the engine KV, nothing local.

    Each call reads the verified session of the running request, so a conversation
    belongs to the caller that opened it (ARBORA-A2A-R001.3).
    """
    from arbora.a2a.storage import EngineA2AStorage, engine_kv_send
    from arbora.security.session import resolve_session

    def session() -> Any:
        return resolve_session(required_scope="kg:read")

    return EngineA2AStorage(
        engine_kv_send(engine_provider),
        tenant=lambda: str(session().tenant),
        principal=lambda: str(session().actor.actor_id),
    )


def mount_sdk_a2a(
    mcp: Any,
    tools: HostTools,
    *,
    build_toolset: Callable[[], Any],
    engine_provider: Callable[[], Any],
    stream_handler: Any = None,
    keys: JwksCache,
) -> Any:
    """Mount the SDK A2A application for the fleet agent behind admission.

    ``stream_handler`` is the shared A2A JSON-RPC handler: the route serves
    ``message/stream`` and ``tasks/resubscribe`` through it and the one event
    log, never through the SDK application (ARBORA-A2A-R001.4.1).
    """
    from fasta2a.broker import InMemoryBroker

    from arbora import __version__
    from arbora.a2a.application import (
        AmbientA2AAuthenticator,
        caller_session_scope,
    )
    from arbora.a2a.sdk_app import create_admitted_a2a_app

    base = str(read_env("ARBORA_PUBLIC_URL") or "").rstrip("/")
    from arbora.modality.agent_input import MediaNormalizingAgent

    storage = _engine_storage(engine_provider)
    admitted = create_admitted_a2a_app(
        agent=MediaNormalizingAgent(
            fleet_agent(build_toolset),
            prompt_normalizer(engine_provider, tools.verbs),
        ),
        authenticator=AmbientA2AAuthenticator(keys),
        bind_caller=caller_session_scope,
        stream_handler=stream_handler,
        name="arbora",
        url=f"{base}{A2A_PATH}",
        version=__version__,
        storage=storage,
        list_contexts=storage.list_contexts,
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


def _engine_probe(
    engine_provider: Callable[[], Any],
) -> Callable[[Any], dict[str, Any]]:
    """The readiness probe of the host's engine, one query under the session."""

    def probe(session: Any) -> dict[str, Any]:
        engine = engine_provider()
        if engine is None:
            return {"status": "unhealthy", "reason": "no engine is bound"}
        from arbora.security.session import use_session

        if session is None:
            engine.query_cypher(ENGINE_PROBE_QUERY, {})
        else:
            with use_session(session):
                engine.query_cypher(ENGINE_PROBE_QUERY, {})
        return {"status": "ok"}

    return probe


def _fleet_probe(gateway: Any) -> Callable[[Any], dict[str, Any]]:
    """The fleet supervision probe over the health the gateway field holds."""

    def probe(session: Any) -> dict[str, Any]:
        return _fleet_health(gateway, session)

    return probe


def _fleet_health(gateway: Any, session: Any) -> dict[str, Any]:
    from arbora.gateway import fleet as fleet_api
    from arbora.security.session import use_session

    def _collect() -> Any:
        return fleet_api.collect_health(gateway)

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


def bind_health_probes(
    health: HealthState, engine_provider: Callable[[], Any], gateway: Any
) -> None:
    """Bind the mandatory readiness probes and the source-provenance probe."""
    from arbora.deployment.live_mount_guard import source_provenance_probe

    health.probes.bind("engine", _engine_probe(engine_provider))
    health.probes.bind("source_provenance", source_provenance_probe)
    health.probes.bind("fleet_supervision", _fleet_probe(gateway))
