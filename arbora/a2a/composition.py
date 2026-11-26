"""Composition root for the Arbora A2A facade."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from arbora.core.settings import runtime_config
from arbora.run_origin import RunOriginRegistry

from .application import A2AAuthenticator, create_a2a_application
from .authority import WorkItemA2AAuthority
from .engine_decider import EngineHarnessConfigDecider
from .event_log import A2AEventLog, A2AEventStreams, StreamClock
from .history import EngineTransitionHistory
from .media_refs import SessionMediaRefStore
from .push import PushDeliveryWorker
from .routing import OrchestratorA2ARouter
from .service import A2ACardMetadata, A2AService

__all__ = ["A2AComposition", "build_a2a_service", "compose_a2a"]


def _tenant() -> str:
    from arbora.security.session import current_session

    return str(getattr(current_session(), "tenant", ""))


def _principal() -> str:
    from arbora.security.session import current_session

    actor = getattr(current_session(), "actor", None)
    return str(getattr(actor, "actor_id", ""))


def _principal_kind() -> str:
    """The verified caller's principal class: ``human`` or ``service``."""
    from arbora.security.actor_context import ActorType
    from arbora.security.session import current_session

    kind = getattr(getattr(current_session(), "actor", None), "actor_type", None)
    return {ActorType.HUMAN: "human", ActorType.AUTOMATED_SERVICE: "service"}.get(
        kind, str(kind)
    )


def stream_buffer_events(config: object) -> int:
    """``a2a.stream_buffer_events``: the slow-consumer bound, from 8 to 4096."""
    value = getattr(config, "a2a_stream_buffer_events", 256)
    if type(value) is not int or not 8 <= value <= 4_096:
        raise ValueError("a2a_stream_buffer_events must be an integer from 8 to 4096")
    return value


def _harness_decider(
    decide: Callable[[dict[str, Any]], Any] | None,
    feature_schema: Callable[[str], Mapping[str, Any]] | None,
    principal: Callable[[], str] | None,
) -> EngineHarnessConfigDecider | None:
    """The engine harness decider, present only when a ``Decide`` port is wired."""
    if decide is None:
        return None
    return EngineHarnessConfigDecider(
        decide,
        feature_schema=feature_schema or (lambda tenant: {}),
        principal=principal or _principal,
    )


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
    decide: Callable[[dict[str, Any]], Any] | None = None,
    feature_schema: Callable[[str], Mapping[str, Any]] | None = None,
    decision_principal: Callable[[], str] | None = None,
    provisioning: Any = None,
    capacity: Any = None,
) -> A2AService:
    """One service over the WorkItem authority and its durable event cursor.

    The event log is the streaming authority: the card advertises
    ``streaming`` because this function wires it (ARBORA-A2A-R014).
    """
    from .authority import _RUN_LEASE_SECONDS
    from .run_delegation import engine_delegation_reader, engine_run_delegations

    log = A2AEventLog(engine_provider, clock=clock)
    authority = WorkItemA2AAuthority(
        engine_provider,
        # PIN-LAG: None until the installed engine serves agent-run delegations.
        delegations=engine_run_delegations(
            engine_provider, clock=time.time, lease_s=_RUN_LEASE_SECONDS
        ),
        delegation_reader=engine_delegation_reader(engine_provider),
        origins=origins,
        task_plan_config=task_plan_config,
        hosted_invocation=hosted_invocation,
        events=log,
    )
    push = PushDeliveryWorker(authority)
    authority.push = push
    authority.provisioning = provisioning
    return A2AService(
        authority=authority,
        push=push,
        router=OrchestratorA2ARouter(
            engine_provider,
            decide=decide,
            feature_schema=feature_schema,
            tenant=_tenant,
            principal=decision_principal or _principal,
            provisioning=provisioning,
            capacity=capacity,
        ),
        harness_decider=_harness_decider(decide, feature_schema, decision_principal),
        principal_kind=_principal_kind,
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
    decide: Callable[[dict[str, Any]], Any] | None = None,
    feature_schema: Callable[[str], Mapping[str, Any]] | None = None,
    decision_principal: Callable[[], str] | None = None,
    provisioning: Any = None,
    capacity: Any = None,
) -> A2AComposition:
    """Compose one projection over the existing WorkItem/dispatch authorities."""
    service = build_a2a_service(
        engine_provider=engine_provider,
        origins=origins,
        card_metadata=card_metadata,
        task_plan_config=task_plan_config,
        hosted_invocation=hosted_invocation,
        clock=clock,
        decide=decide,
        feature_schema=feature_schema,
        decision_principal=decision_principal,
        provisioning=provisioning,
        capacity=capacity,
    )
    return A2AComposition(
        application=create_a2a_application(
            service=service,
            authenticator=authenticator,
        ),
        service=service,
    )
