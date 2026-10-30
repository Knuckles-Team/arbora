"""Composition root for the Arbora unary A2A facade."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from arbora.control_plane.runs.admission import CapacityAcquisition

from .application import A2AAuthenticator, create_a2a_application
from .authority import WorkItemA2AAuthority
from .routing import OrchestratorA2ARouter
from .service import A2ACardMetadata, A2AService

__all__ = [
    "A2AComposition",
    "CapacityGatedPublisher",
    "PublicationOutcome",
    "compose_a2a",
]


@dataclass(frozen=True)
class A2AComposition:
    application: Any
    service: A2AService


def compose_a2a(
    *,
    engine_provider: Callable[[], Any],
    authenticator: A2AAuthenticator,
    card_metadata: A2ACardMetadata | None = None,
    task_plan_config: Any = None,
    hosted_invocation: Any = None,
) -> A2AComposition:
    """Compose one projection over the existing WorkItem/dispatch authorities."""
    service = A2AService(
        authority=WorkItemA2AAuthority(
            engine_provider,
            task_plan_config=task_plan_config,
            hosted_invocation=hosted_invocation,
        ),
        router=OrchestratorA2ARouter(engine_provider),
        card_metadata=card_metadata or A2ACardMetadata(),
    )
    return A2AComposition(
        application=create_a2a_application(
            service=service,
            authenticator=authenticator,
        ),
        service=service,
    )


@dataclass(frozen=True)
class PublicationOutcome:
    """Result of one evaluate/commit/capacity/publish attempt."""

    published: bool
    redecision_required: bool = False
    published_value: Any = None


class CapacityGatedPublisher:
    """Evaluate, commit with synthesis evidence, then publish through capacity.

    ARBORA-FLEET-R009.2: a candidate run is evaluated and committed with its
    synthesis evidence, then publishes only after an all-or-nothing capacity
    acquisition (``CapacityAcquisition``). A first denial surfaces the single
    re-decision to the caller (``redecision_required``); a second denial fails
    closed with ``CapacityDeniedError``. ``stop`` releases the run's hold.
    """

    def __init__(self, capacity: CapacityAcquisition) -> None:
        self._capacity = capacity

    def publish(
        self,
        run_id: str,
        resources: dict[str, int],
        *,
        evaluate: Callable[[], Any],
        commit: Callable[[Any], Any],
        publish: Callable[[Any], Any],
    ) -> PublicationOutcome:
        candidate = evaluate()
        committed = commit(candidate)
        if not self._capacity.acquire(run_id, resources):
            return PublicationOutcome(published=False, redecision_required=True)
        try:
            value = publish(committed)
        except BaseException:
            self._capacity.release(run_id)
            raise
        return PublicationOutcome(published=True, published_value=value)

    def stop(self, run_id: str) -> None:
        self._capacity.release(run_id)
