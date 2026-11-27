"""The finance, market and city ports the served invocation binds (ARBORA-HOST-R038.11)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


def _with_finance_read_port(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Bind the marketplace read port (ARBORA-DATA-MARKET-R003.6).

    The port reaches the venue only through the governed fleet call; it holds no
    provider client and no order method.
    """
    if bindings is None or "finance_read_port" in bindings:
        return bindings
    from arbora.api.domain.finance.fleet_read_port import FleetFinanceReadPort

    return {**bindings, "finance_read_port": FleetFinanceReadPort()}


def _with_market_compute_port(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Bind the market compute port (ARBORA-DATA-MARKET-R003.13); it sends as the caller."""
    if bindings is None or "market_compute_port" in bindings:
        return bindings
    from arbora.finance.compute_port import EngineMarketComputePort

    return {**bindings, "market_compute_port": EngineMarketComputePort()}


def _with_finance_import_ports(bindings: Mapping[str, Any]) -> Mapping[str, Any]:
    """Compose the CSV import ports (ARBORA-DATA-MARKET-R009.6).

    The source reads the uploaded blob through the upload path and the sink sends the
    engine activity ingest; a port the deployment binds itself wins.
    """
    from arbora.finance.import_ports import BlobImportSource, EngineActivitySink

    return {
        "finance_import_source": BlobImportSource(),
        "finance_activity_sink": EngineActivitySink(),
        **bindings,
    }


def _delegated_subject_check(
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
    session: Callable[[], Any],
) -> Any:
    """The service executor's subject check, with the finance delegation path.

    ARBORA-DATA-MARKET-R006: a scheduled paper fill passes the same ownership check as any
    service operation, admitted by the owner's delegated grant that the engine confirms.
    Everything else falls to the default refusal until a subject authority is composed.
    """
    from arbora.api.domain.finance.delegation import DelegatedSubjectAccess
    from arbora.api.invoke.bootstrap import deny_subject_access

    async def wire(method: str, params: dict[str, Any]) -> Any:
        from medulla.generated import send_by_id

        from arbora.api.ops.engine_calls import decode

        client = await pool.connected_client(
            connect_context(str(session().tenant)), graph
        )
        return decode(await send_by_id(client, method, params, graph))

    return DelegatedSubjectAccess(wire, deny_subject_access)


def _with_finance_change_ports(
    bindings: Mapping[str, Any],
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
    session: Callable[[], Any],
) -> Mapping[str, Any]:
    """Compose the wallet authority and the order and position change door.

    ARBORA-DATA-MARKET-R007.3: ``wallet.operate`` verifies the SDK proof against engine key
    trust, asks the engine's grant ledger and reserves the effect once in the engine; the SDK
    shapes it needs are guarded by the pin (a pinned release refuses naming the symbol). A
    port the deployment binds itself wins.
    """
    import time

    from arbora.finance.wallet_port import (
        EngineWalletOperations,
        FleetFinanceChangePort,
    )

    async def client() -> Any:
        return await pool.connected_client(
            connect_context(str(session().tenant)), graph
        )

    async def wire(method: str, params: dict[str, Any]) -> Any:
        from medulla.generated import send_by_id

        from arbora.api.ops.engine_calls import decode

        return decode(await send_by_id(await client(), method, params, graph))

    return {
        "wallet_operations": EngineWalletOperations(
            wire, client, clock_ms=lambda: time.time_ns() // 1_000_000
        ),
        "finance_change_port": FleetFinanceChangePort(),
        **bindings,
    }


def _with_finance_schedule_host(
    bindings: Mapping[str, Any],
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
    session: Callable[[], Any],
) -> Mapping[str, Any]:
    """Compose the finance schedule host so the Loops worker runs finance schedules.

    ARBORA-DATA-MARKET-R002: the host reads schedule entries, delegated grants and
    receipts from the engine as the worker's verified service session. A host the
    deployment binds itself wins.
    """
    if bindings.get("finance_schedule_host") is not None:
        return bindings
    from arbora.api.mcp.caller import caller_from_session
    from arbora.finance.paper_door import LatePaperDoor
    from arbora.finance.schedule_host import EngineFinanceScheduleHost

    tenant = str(session().tenant)

    async def send(method: str, params: dict[str, Any]) -> Any:
        from medulla.generated import send_by_id

        client = await pool.connected_client(connect_context(tenant), graph)
        return await send_by_id(client, method, params, graph)

    scheduler = str(session().actor.actor_id)
    # ARBORA-DATA-MARKET-R006: a paper fill is the one served paper order handler, run as
    # the host service caller; the invoke services bind it once they exist.
    paper = LatePaperDoor(
        lambda: caller_from_session(session(), credential_kind="service")
    )
    return {
        **bindings,
        "finance_scheduler_principal": scheduler,
        "finance_paper_door": paper,
        "finance_schedule_host": EngineFinanceScheduleHost(
            send, tenant, scheduler=scheduler, paper_order=paper
        ),
    }


def _with_city_ports(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Bind the graph city port to the four city services (ARBORA-FLEET-R086).

    One port serves each city read and write from the tenant graph through the
    engine's existing node reads and writes as the verified caller; the engine has
    no spatial method. A service the host already bound is kept.
    """
    if bindings is None:
        return bindings
    from arbora.fleet.city_ports import CITY_SERVICES, GraphCityPort

    port = GraphCityPort()
    return {**{name: port for name in CITY_SERVICES}, **bindings}
