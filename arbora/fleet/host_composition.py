"""The host composes the onboarding-time ports (ARBORA-FLEET-R002.5, R045, R058).

``start_fleet_onboarding`` calls :func:`compose_onboarding_ports` once at boot.
It builds the definition admission over the engine port, declares the
supervised ingest worker unit for the served engine and declares the
data-prep capabilities of ``data_prep.yml``. Each step that the pinned engine
or the config does not support is skipped and logged; none stops boot.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from arbora.fleet.definition_admission import DefinitionAdmission
from arbora.fleet.engine_admission import EngineAdmission
from arbora.fleet.ingest_worker_unit import (
    IngestWorkerUnit,
    declare_ingest_worker_unit,
)

logger = logging.getLogger(__name__)

__all__ = [
    "OnboardingPorts",
    "compose_definition_admission",
    "compose_onboarding_ports",
    "declare_data_prep_at_boot",
    "declare_host_data_prep",
]


@dataclass(frozen=True)
class OnboardingPorts:
    """What the host composed for one onboarding service."""

    definition_admission: DefinitionAdmission | None
    ingest_worker: IngestWorkerUnit | None = None


def _engine_send(client: Any, graph: str) -> Callable[..., Awaitable[Any]]:
    async def send(method: str, params: dict[str, Any], tenant: str) -> Any:
        from medulla.generated import send_by_id

        from arbora.api.ops._engine import decode

        return decode(await send_by_id(client, method, params, graph))

    return send


def compose_definition_admission(engine: Any, session: Any) -> DefinitionAdmission:
    """The definition admission over the tenant client: engine port and receipt sink."""
    from arbora.api.task_plan_schema import published_pin
    from arbora.deployment.semantic_provisioning import bind_session_graph

    graph = str(session.graph)
    client = engine.graph_compute.for_graph(graph).async_client

    async def write(ntriples: str, tenant: str) -> None:
        from medulla.generated.graph import send_add_triples

        with bind_session_graph(graph):
            await send_add_triples(client, {"ntriples": ntriples}, graph)

    port = EngineAdmission(
        _engine_send(client, graph), lambda tenant: published_pin(None, tenant)
    )
    return DefinitionAdmission(port, write)


async def declare_host_data_prep(engine: Any, session: Any) -> list[str]:
    """Declare ``data_prep.yml`` to the engine; an absent file declares nothing."""
    from arbora.deployment.data_prep import declare_data_prep
    from arbora.pin_lag.medulla import require_methods

    require_methods("DataPrepDeclare")
    graph = str(session.graph)
    send = _engine_send(engine.graph_compute.for_graph(graph).async_client, graph)

    async def declare(request: dict[str, Any]) -> Any:
        return await send("DataPrepDeclare", request["payload"], request["tenant"])

    return await declare_data_prep(declare, tenant=str(session.tenant))


def _served_engine_endpoint() -> str:
    """The first ``GRAPH_SERVICE_ENDPOINTS`` entry, the engine this host serves."""
    from arbora.core.settings import setting

    value = str(setting("GRAPH_SERVICE_ENDPOINTS", "") or "")
    return value.split(",")[0].strip()


def declare_data_prep_at_boot(engine: Any, session: Any) -> list[str]:
    """Run the one-time data-prep declaration on its own loop; a fault is logged.

    The warm-up thread has no running loop, so the declaration owns one, scoped
    like the catalog composition (ARBORA-FLEET-R078). An absent file or a pinned
    engine without ``DataPrepDeclare`` declares nothing and never stops boot.
    """
    import asyncio

    async def run() -> list[str]:
        from arbora.epistemic import temporary_client_scope

        async with temporary_client_scope():
            return await declare_host_data_prep(engine, session)

    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - boot never fails on data prep
        logger.warning("data-prep declaration skipped: %s", exc)
        return []


def compose_onboarding_ports(
    engine: Any, session: Any, *, engine_endpoint: str = ""
) -> OnboardingPorts:
    """Compose the admission and the worker unit; data prep runs on the loop."""
    unit = None
    endpoint = engine_endpoint or _served_engine_endpoint()
    try:
        unit = declare_ingest_worker_unit(engine_endpoint=endpoint)
    except ValueError:
        logger.info("ingest worker unit not declared: no served engine endpoint")
    return OnboardingPorts(_admission_or_none(engine, session), unit)


def _admission_or_none(engine: Any, session: Any) -> DefinitionAdmission | None:
    """The definition admission, or ``None`` for an engine with no graph compute.

    An engine that cannot open a graph client has no admission to compose; onboarding
    then fails a bound definition closed rather than the boot stopping.
    """
    try:
        return compose_definition_admission(engine, session)
    except AttributeError:
        logger.info("definition admission not composed: the engine has no graph client")
        return None
