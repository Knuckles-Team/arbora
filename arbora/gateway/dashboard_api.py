"""Service dashboard REST and WebSocket surface (ARBORA-HOST-R019).

CONCEPT:MERISTEM-OS.config.gateway-service-dashboard — Gateway Service Dashboard

overstory commit ``08ab4ea`` removed its own ``/api/dashboard`` mount and the
``/ws/dashboard`` stream so Arbora could own them through the application
composer. Arbora never registered them, so the browser received 404 for the
REST reads and a bare 403 for the unmatched WebSocket handshake.

This module ports the meristem ``gateway/api.py`` dashboard router and
the overstory WebSocket handler onto Arbora's own gateway aggregator and
daemon. Widget data is read through the served operation
``ecosystem.widgets.read`` as the caller (ARBORA-OPS-R914). :func:`register_dashboard_routes` mounts both from
:func:`arbora.gateway.graph_api.register_graph_routes`, so the routes share
the identity middleware and the WebUI authorization middleware that guard
every other ``/api`` route. The WebSocket handler performs no extra auth
check; the middleware chain decides who may connect.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.websockets import WebSocket, WebSocketDisconnect

from arbora.gateway.aggregator import WIDGET_READ_OP, Aggregator, WidgetReader
from arbora.gateway.config import ConfigManager
from arbora.gateway.models import DashboardLayout, WidgetData
from arbora.gateway.route_marks import build_router, route
from arbora.gateway.state import GatewayState, gateway_of
from arbora.security.error_surface import public_error_payload

__all__ = [
    "DASHBOARD_WS_PATH",
    "DashboardResponse",
    "build_dashboard_router",
    "fetch_dashboard_subset",
    "get_full_dashboard",
    "register_dashboard_routes",
]

logger = logging.getLogger(__name__)

DASHBOARD_WS_PATH = "/ws/dashboard"
_SERVICE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_HYDRATION_SOURCE_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")
#: Bound on how many widget ids one ``subscribe`` message may name.
_MAX_SUBSCRIBE_WIDGET_IDS = 256
#: Bound on census pages read to list the widgets.
_CENSUS_PAGES = 20
#: Push cadence after the initial snapshot. Matches the aggregator read cache.
_WS_PUSH_INTERVAL_SECONDS = 15.0
_READ_CAPABILITIES = frozenset(
    {"gateway:read", "gateway:write", "gateway:admin", "admin"}
)
_WRITE_CAPABILITIES = frozenset({"gateway:write", "gateway:admin", "admin"})


def _dashboard_capabilities(request: Request) -> set[str] | None:
    claims = getattr(request.state, "user_claims", None)
    if not claims or claims.get("auth_type") == "api_key":
        return None
    try:
        from arbora.core.settings import runtime_config as config
        from arbora.security.identity import (
            base_capabilities,
            normalize_identity,
        )

        return set(
            base_capabilities(
                normalize_identity(claims), config.identity_group_capability_map
            )
        )
    except Exception:
        raise HTTPException(
            status_code=403, detail="dashboard capability required"
        ) from None


async def _require_dashboard_read(request: Request) -> None:
    if request.url.path.endswith("/health"):
        return
    capabilities = _dashboard_capabilities(request)
    if capabilities is not None and not capabilities & _READ_CAPABILITIES:
        raise HTTPException(status_code=403, detail="dashboard capability required")


def _require_dashboard_write(request: Request) -> None:
    capabilities = _dashboard_capabilities(request)
    if capabilities is not None and not capabilities & _WRITE_CAPABILITIES:
        raise HTTPException(
            status_code=403, detail="dashboard write capability required"
        )


def _get_aggregator(request: Any) -> Aggregator:
    """The host's aggregator, created on first request.

    Layout writes persist to the shared YAML file, so replicas converge on their next
    read.
    """
    return gateway_of(request).aggregator.get_or_build(
        lambda: Aggregator(config_manager=ConfigManager())
    )


class DashboardResponse(BaseModel):
    layout: DashboardLayout
    data: dict[str, WidgetData]


class WidgetListItem(BaseModel):
    widget_type: str
    display_name: str
    icon: str
    category: str
    description: str
    supports_websocket: bool


@route("GET", "/layout")
async def get_layout(request: Request) -> DashboardLayout:
    """Return the current dashboard layout configuration."""
    return _get_aggregator(request).get_layout()


@route("PUT", "/layout")
async def save_layout(layout: DashboardLayout, request: Request) -> dict[str, str]:
    """Persist a new dashboard layout configuration."""
    _require_dashboard_write(request)
    _get_aggregator(request).save_layout(layout)
    return {"status": "saved"}


@route("GET", "/data")
async def get_all_data(request: Request) -> dict[str, WidgetData]:
    """Fetch data from every active widget."""
    return await _get_aggregator(request).fetch_all(_reader(request))


@route("GET", "/data/{service_id}")
async def get_service_data(service_id: str, request: Request) -> WidgetData:
    """Fetch data for one service."""
    if not _SERVICE_ID_RE.fullmatch(service_id):
        raise HTTPException(status_code=404, detail="service not found")
    data = await _get_aggregator(request).fetch_one(service_id, _reader(request))
    if data.status == "error" and "not found" in (data.error or ""):
        raise HTTPException(status_code=404, detail="service not found")
    return data


@route("GET", "/full")
async def get_full_dashboard(request: Request) -> DashboardResponse:
    """Return layout and data in one request for the initial page load."""
    aggregator = _get_aggregator(request)
    layout = aggregator.get_layout()
    data = await aggregator.fetch_all(_reader(request))
    return DashboardResponse(layout=layout, data=data)


async def fetch_dashboard_subset(
    widget_ids: set[str], request: Any
) -> dict[str, WidgetData]:
    """Fetch exactly the named widgets concurrently through ``fetch_one``."""
    aggregator = _get_aggregator(request)
    read = _reader(request)
    ordered_ids = list(widget_ids)
    results = await asyncio.gather(
        *(aggregator.fetch_one(widget_id, read) for widget_id in ordered_ids)
    )
    return dict(zip(ordered_ids, results, strict=True))


@route("GET", "/data-subset")
async def get_dashboard_subset(
    request: Request,
    widget_id: Annotated[list[str] | None, Query()] = None,
) -> dict[str, WidgetData]:
    """REST twin of :func:`fetch_dashboard_subset`."""
    return await fetch_dashboard_subset(set(widget_id or ()), request)


@route("GET", "/widgets")
async def list_available_widgets(request: Request) -> list[WidgetListItem]:
    """List every connector of the fleet census a widget can read."""
    items: list[WidgetListItem] = []
    cursor: str | None = None
    for _page in range(_CENSUS_PAGES):
        arguments: dict[str, Any] = {"summary": True, "limit": 100}
        if cursor:
            arguments["cursor"] = cursor
        census = await _served(request, "fleet.catalog.list", arguments)
        items.extend(
            WidgetListItem(
                widget_type=row["server"].replace("-", "_"),
                display_name=row["server"],
                icon="",
                category="",
                description="",
                supports_websocket=False,
            )
            for row in census.get("servers", ())
            if isinstance(row, dict) and isinstance(row.get("server"), str)
        )
        cursor = census.get("next_cursor")
        if not cursor:
            break
    return items


@route("GET", "/health")
async def health_check(request: Request) -> JSONResponse:
    """Liveness: always 200, with the shared runtime health report as body."""
    from arbora.observability.runtime_health import collect_health_async

    report = await collect_health_async(request.app.state.host.observability.health)
    return JSONResponse(report, headers={"Cache-Control": "no-store"})


def _engine_probe(gateway: GatewayState) -> dict[str, Any]:
    from arbora.observability.readiness import check

    if gateway.application().engine() is not None:
        return check("ready")
    return check("unavailable", reason="no active engine")


@route("GET", "/readiness")
async def readiness_check(request: Request) -> JSONResponse:
    """Readiness: the joined snapshot of the engine probe and registered probes."""
    from arbora.observability.readiness import (
        collect_readiness_snapshot,
        is_snapshot_ready,
    )

    gateway = gateway_of(request)
    probes = {
        "engine": lambda: _engine_probe(gateway),
        **request.app.state.host.observability.health.readiness_probes.copy(),
    }
    snapshot = await asyncio.to_thread(
        collect_readiness_snapshot, probes, required=("engine",)
    )
    return JSONResponse(
        snapshot,
        status_code=200 if is_snapshot_ready(snapshot) else 503,
        headers={"Cache-Control": "no-store"},
    )


@route("GET", "/discover")
async def discover_services() -> DashboardLayout:
    """Auto-discover services from the MCP configuration as a layout."""
    return ConfigManager()._auto_discover()


def _gateway_engine_or_none(request: Request) -> Any:
    """The gateway port's engine, or ``None`` when no engine is composed."""
    try:
        return gateway_of(request).application().engine()
    except Exception:
        return None


@route("GET", "/daemon/status")
async def daemon_status(request: Request) -> dict[str, Any]:
    """Return the consolidated KG host daemon status."""
    from arbora.gateway.daemon import daemon_status as _status

    return _status(_gateway_engine_or_none(request))


@route("GET", "/daemon/shards")
async def daemon_shards(request: Request) -> dict[str, Any]:
    """Return the engine shard topology and per-shard reachability.

    The engine's member list, reachability and snapshot epochs are returned
    unchanged; arbora computes no route (ARBORA-OPS-R039.6).
    """
    from medulla.placement import PlacementStaleError

    unavailable = HTTPException(status_code=503, detail={"code": "UNAVAILABLE"})
    try:
        engine = gateway_of(request).application().engine()
    except Exception:
        raise unavailable from None
    topology = getattr(
        getattr(engine, "placement_directory", None), "shard_topology", None
    )
    if not callable(topology):
        raise unavailable
    try:
        if inspect.iscoroutinefunction(topology):
            return dict(await topology())
        return dict(await asyncio.to_thread(topology))
    except PlacementStaleError:
        raise HTTPException(
            status_code=409, detail={"code": "PLACEMENT_STALE"}
        ) from None
    except Exception:
        logger.warning("engine shard topology read failed", exc_info=True)
        raise unavailable from None


@route("POST", "/daemon/start")
async def daemon_start(request: Request) -> dict[str, Any]:
    """Ensure the consolidated KG host daemon runs in this process."""
    _require_dashboard_write(request)
    from arbora.gateway.daemon import daemon_status as _status
    from arbora.gateway.daemon import start_host_daemon

    start_host_daemon(request.app.state.host.authority.signer)
    return _status(_gateway_engine_or_none(request))


#: Refusal codes of a served operation, mapped to the dashboard's HTTP statuses.
_REFUSAL_STATUS = {
    "INVALID_ARGUMENT": 400,
    "NOT_FOUND": 400,
    "UNAUTHENTICATED": 401,
    "SCOPE_REQUIRED": 403,
    "PRINCIPAL_NOT_ALLOWED": 403,
    "SURFACE_NOT_ALLOWED": 403,
}


async def _served(
    request: Request,
    op_id: str,
    params: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> Any:
    """Call one served operation as the dashboard caller (ARBORA-OPS-R039.7).

    The call goes through the one invoke pipeline under the caller's own
    verified identity. Refusals keep the dashboard's status codes: 400 for an
    invalid or unknown source, 401/403 for authority, and 500 for the rest,
    including a gateway that has no application composed.
    """
    from arbora.api.http.auth import HTTPAuthenticationError
    from arbora.api.invoke.pipeline import OperationRefused

    try:
        value = (
            await gateway_of(request)
            .application()
            .invoke_operation(
                op_id, params, request=request, idempotency_key=idempotency_key
            )
        )
    except HTTPAuthenticationError as exc:
        raise HTTPException(
            status_code=401, detail=public_error_payload(exc, logger=logger)
        ) from None
    except OperationRefused as exc:
        status = _REFUSAL_STATUS.get(exc.code, 500)
        raise HTTPException(
            status_code=status,
            detail=public_error_payload(
                exc,
                logger=logger,
                code="invalid_request" if status == 400 else "operation_failed",
            ),
        ) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=public_error_payload(exc, logger=logger, code="invalid_request"),
        ) from None
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=public_error_payload(exc, logger=logger)
        ) from None
    if isinstance(value, dict) and set(value) == {"value"}:
        return value["value"]
    return value


def _reader(request: Any) -> WidgetReader:
    """``ecosystem.widgets.read`` as the caller of ``request`` (ARBORA-OPS-R914)."""

    async def read(widget: str) -> Any:
        return await _served(request, WIDGET_READ_OP, {"widget": widget})

    return read


async def _sync_source(request: Request, source_id: str) -> dict[str, Any]:
    receipt = await _served(
        request,
        "ingest.sources.sync",
        {"source_id": source_id, "mode": "incremental"},
        idempotency_key=f"dashboard-hydrate-{uuid.uuid4().hex}",
    )
    return dict(receipt)


async def _source_ids(request: Request) -> list[str]:
    inventory = await _served(request, "ingest.sources.list", {})
    return [str(source["source_id"]) for source in inventory.get("sources", ())]


@route("POST", "/hydrate/{source}")
async def trigger_hydration(source: str, request: Request) -> dict[str, Any]:
    """Trigger hydration for one external source through served ingest."""
    _require_dashboard_write(request)
    if not _HYDRATION_SOURCE_RE.fullmatch(source):
        raise HTTPException(status_code=422, detail="invalid hydration source")
    return await _sync_source(request, source)


@route("POST", "/hydrate")
async def trigger_all_hydration(request: Request) -> dict[str, Any]:
    """Trigger hydration for every configured source in sequence."""
    _require_dashboard_write(request)
    return {
        source_id: await _sync_source(request, source_id)
        for source_id in await _source_ids(request)
    }


@route("GET", "/hydration-status")
async def get_hydration_status(request: Request) -> dict[str, Any]:
    """Return one status entry per hydration source."""
    status: dict[str, Any] = {}
    for source_id in await _source_ids(request):
        record = await _served(
            request, "ingest.sources.status", {"source_id": source_id}
        )
        status[source_id] = {
            "configured": True,
            "last_job_id": record.get("last_job_id"),
        }
    return status


def parsed_widget_subscription(raw: str) -> set[str] | None:
    """Return the widget ids a ``subscribe`` message names, else ``None``."""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict) or parsed.get("type") != "subscribe":
        return None
    widget_ids = parsed.get("widget_ids")
    if not isinstance(widget_ids, list):
        return None
    return {
        widget_id
        for widget_id in widget_ids[:_MAX_SUBSCRIBE_WIDGET_IDS]
        if isinstance(widget_id, str)
    }


async def _widgets_for(
    subscription: set[str] | None, websocket: WebSocket
) -> dict[str, WidgetData]:
    if subscription is None:
        return (await get_full_dashboard(websocket)).data  # type: ignore[arg-type]
    return await fetch_dashboard_subset(subscription, websocket)


async def _next_subscription(
    websocket: WebSocket, current: set[str] | None
) -> set[str] | None:
    """Wait one push interval for a ``subscribe`` message."""
    try:
        raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=_WS_PUSH_INTERVAL_SECONDS
        )
    except TimeoutError:
        return current
    subscription = parsed_widget_subscription(raw)
    return current if subscription is None else subscription


async def _stream_dashboard(websocket: WebSocket) -> None:
    """Send a snapshot, then periodic updates shaped like ``/full``'s data.

    Each message carries a per-connection ``stream_id`` and a monotonic
    ``sequence``. A new ``stream_id`` tells a client to treat the snapshot as a
    reset. A ``subscribe`` message scopes later pushes to the named widgets.
    """
    stream_id = uuid.uuid4().hex
    sequence = 0
    message_type = "snapshot"
    subscription: set[str] | None = None
    while True:
        widgets = await _widgets_for(subscription, websocket)
        sequence += 1
        await websocket.send_json(
            {
                "type": message_type,
                "stream_id": stream_id,
                "sequence": sequence,
                "data": {
                    widget_id: widget.model_dump(mode="json")
                    for widget_id, widget in widgets.items()
                },
            }
        )
        message_type = "update"
        subscription = await _next_subscription(websocket, subscription)


async def dashboard_websocket(websocket: WebSocket) -> None:
    """``/ws/dashboard``: stream dashboard widget data to the browser."""
    await websocket.accept()
    try:
        await _stream_dashboard(websocket)
    except WebSocketDisconnect as exc:
        logger.info("/ws/dashboard closed by client: code=%s", exc.code)
    except Exception:
        logger.warning("/ws/dashboard stream failed", exc_info=True)


def build_dashboard_router() -> APIRouter:
    """The dashboard router, built for one composition."""
    return build_router(
        __name__, tags=["dashboard"], dependencies=[Depends(_require_dashboard_read)]
    )


def register_dashboard_routes(app: Any, *, prefix: str = "/api") -> None:
    """Mount ``{prefix}/dashboard/*`` and ``/ws/dashboard`` onto ``app``."""
    app.include_router(build_dashboard_router(), prefix=f"{prefix}/dashboard")
    if not any(getattr(r, "path", None) == DASHBOARD_WS_PATH for r in app.routes):
        app.add_api_websocket_route(DASHBOARD_WS_PATH, dashboard_websocket)
    logger.info("Mounted service dashboard API at %s/dashboard", prefix)
