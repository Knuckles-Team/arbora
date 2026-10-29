"""Native startup composition for EG and AU catalog read authorities."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from arbora.fleet.catalog_reader import (
    DeferredFleetCatalogReader,
    FleetCatalogReader,
    ReadContext,
)
from arbora.fleet.epistemic_adapter import GeneratedFleetCatalogPort, GraphBinder
from arbora.gateway.enhanced_catalog_api import (
    AgentCatalogReadPort,
    EnhancedCatalogAuthority,
    WorkflowCatalogReadPort,
    configure_enhanced_catalog,
)
from arbora.security.anonymous import session_context
from arbora.semantic_content import (
    required_content_connectors,
    verify_semantic_content,
)

CatalogPortsFactory = Callable[
    [Any, Any], tuple[WorkflowCatalogReadPort, AgentCatalogReadPort]
]


def _public_au_catalog_ports(
    engine: Any, session: Any
) -> tuple[WorkflowCatalogReadPort, AgentCatalogReadPort]:
    """Resolve only AU's public execution/control-plane application seam."""

    from arbora.api.contracts import catalog_read_ports

    return catalog_read_ports(engine, session)


#: How long one verified fleet snapshot is reused (seconds). A full read of a
#: real fleet takes about a minute, and one governed fleet.call admits its item
#: several times; a revoked component stays callable at most this long.
CATALOG_SNAPSHOT_TTL_SETTING = "ARBORA_FLEET_CATALOG_TTL_S"
DEFAULT_CATALOG_SNAPSHOT_TTL_S = 120.0


def _catalog_snapshot_ttl_s() -> float:
    from arbora.core.settings import setting

    raw = str(setting(CATALOG_SNAPSHOT_TTL_SETTING, "") or "").strip()
    try:
        return max(0.0, float(raw)) if raw else DEFAULT_CATALOG_SNAPSHOT_TTL_S
    except ValueError:
        return DEFAULT_CATALOG_SNAPSHOT_TTL_S


def _use_process_session(session: Any) -> AbstractContextManager[object]:
    """Bind the Arbora process session for one browser catalog read."""

    from arbora.api.contracts.session import use_session

    return use_session(session)


def _process_graph_binder(session: Any) -> GraphBinder:
    """Bind the Arbora process session, re-targeted to one graph, for a read.

    The fleet catalog is one process-wide authority (ARBORA-FLEET-R026.2).
    Its read context names the process, its snapshot is shared by every
    caller, and caller admission is Arbora's scope check on each item. The
    engine row-level policy hides process-owned ``:Server`` registrations
    from an ordinary operator session, so a read under the caller's session
    saw no live server and served an empty catalog.
    """

    from arbora.api.contracts.session import use_session

    def bind(graph: str) -> AbstractContextManager[object]:
        return use_session(session.with_graph(graph))

    return bind


def _read_context(session: Any) -> ReadContext:
    claims = session_context(session)
    return ReadContext(
        tenant_id=str(claims["tenant"]),
        principal_id=str(claims["principal"]),
        agent_id=str(claims["agent_id"]),
        audience=str(claims["audience"]),
        policy_version=str(claims["policy_version"]),
    )


async def compose_catalog_authorities(
    *,
    engine: Any,
    session: Any,
    deferred_fleet: DeferredFleetCatalogReader,
    multiplexer: Any,
    catalog_ports_factory: CatalogPortsFactory = _public_au_catalog_ports,
    bind_graph: GraphBinder | None = None,
) -> FleetCatalogReader:
    """Install and prove every catalog authority before service readiness.

    The process engine supplies non-owning, session-routed generated-client
    views. The initial multiplexer refresh is mandatory: an unavailable or
    inconsistent EG/AU authority aborts startup and no static declaration is
    ever consulted.
    """

    bind_graph = bind_graph or _process_graph_binder(session)
    context = _read_context(session)
    # The tenant's data graph is the verified session's graph (AU's tenant
    # graph naming), never the bare tenant id.
    tenant_graph = str(session.graph)
    compute = engine.graph_compute
    tenant_client = compute.for_graph(tenant_graph).async_client
    commons_client = compute.for_graph("__commons__").async_client
    reader = FleetCatalogReader(
        GeneratedFleetCatalogPort(
            tenant_client=tenant_client,
            commons_client=commons_client,
            context=context,
            tenant_graph=tenant_graph,
            bind_graph=bind_graph,
        ),
        snapshot_ttl_s=_catalog_snapshot_ttl_s(),
    )
    with bind_graph(tenant_graph):
        await verify_semantic_content(
            client=tenant_client,
            tenant_id=context.tenant_id,
            graph=tenant_graph,
            connectors=required_content_connectors(),
        )
    workflows, agents = catalog_ports_factory(engine, session)
    deferred_fleet.install(reader)
    configure_enhanced_catalog(
        EnhancedCatalogAuthority(
            fleet=reader,
            workflows=workflows,
            agents=agents,
        ),
        authority_scope=lambda: _use_process_session(session),
    )
    await multiplexer.refresh_engine_catalog()
    return reader


__all__ = ["compose_catalog_authorities"]
