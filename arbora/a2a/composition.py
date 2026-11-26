"""Composition root for the Arbora A2A facade."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from arbora.core.settings import runtime_config
from arbora.run_origin import RunOriginRegistry

from .application import A2AAuthenticator, create_a2a_application
from .authority import WorkItemA2AAuthority
from .event_log import A2AEventLog, A2AEventStreams, StreamClock
from .history import EngineTransitionHistory
from .media_refs import SessionMediaRefStore
from .routing import OrchestratorA2ARouter
from .service import A2ACardMetadata, A2AService

__all__ = ["A2AComposition", "build_a2a_service", "compose_a2a"]


def stream_buffer_events(config: object) -> int:
    """``a2a.stream_buffer_events``: the slow-consumer bound, from 8 to 4096."""
    value = getattr(config, "a2a_stream_buffer_events", 256)
    if type(value) is not int or not 8 <= value <= 4_096:
        raise ValueError("a2a_stream_buffer_events must be an integer from 8 to 4096")
    return value


@dataclass(frozen=True)
class A2AComposition:
    application: Any
    service: A2AService


def build_a2a_service(
    *,
    engine_provider: Callable[[], Any],
    origins: RunOriginRegistry,
    card_metadata: A2ACardMetadata | None = None,
    task_plan_config: Any = None,
    hosted_invocation: Any = None,
    clock: StreamClock | None = None,
) -> A2AService:
    """One service over the WorkItem authority and its durable event cursor.

    The event log is the streaming authority: the card advertises
    ``streaming`` because this function wires it (ARBORA-A2A-R014).
    """
    log = A2AEventLog(engine_provider, clock=clock)
    authority = WorkItemA2AAuthority(
        engine_provider,
        origins=origins,
        task_plan_config=task_plan_config,
        hosted_invocation=hosted_invocation,
        events=log,
    )
    return A2AService(
        authority=authority,
        router=OrchestratorA2ARouter(engine_provider),
        card_metadata=card_metadata or A2ACardMetadata(),
        streaming=A2AEventStreams(
            log, authority, buffer_events=stream_buffer_events(runtime_config)
        ),
        history=EngineTransitionHistory(authority),
        media_refs=SessionMediaRefStore(engine_provider),
    )


def compose_a2a(
    *,
    engine_provider: Callable[[], Any],
    origins: RunOriginRegistry,
    authenticator: A2AAuthenticator,
    card_metadata: A2ACardMetadata | None = None,
    task_plan_config: Any = None,
    hosted_invocation: Any = None,
    clock: StreamClock | None = None,
) -> A2AComposition:
    """Compose one projection over the existing WorkItem/dispatch authorities."""
    service = build_a2a_service(
        engine_provider=engine_provider,
        origins=origins,
        card_metadata=card_metadata,
        task_plan_config=task_plan_config,
        hosted_invocation=hosted_invocation,
        clock=clock,
    )
    return A2AComposition(
        application=create_a2a_application(
            service=service,
            authenticator=authenticator,
        ),
        service=service,
    )
