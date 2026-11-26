"""Ecosystem widget reads (ARBORA-OPS-R914).

``ecosystem.widgets.read`` projects one widget of the gateway widget registry
into rows. The widget class calls its connector through the fleet. A connector
that is down never fails the call: the reply is ``degraded`` and carries the
last rows read for that tenant and widget, or none. The cache only softens a
down connector; it is never the source of a healthy answer. ``services`` is
read from the fleet status ``children`` list, not from ``servers``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops._nested import invoke_registered
from arbora.api.registry import OpSpec
from arbora.gateway.models import ServiceConfig, WidgetData
from arbora.gateway.registry import Registry, get_registry

SCOPE = "fleet:read"
SERVICES = "services"
_LAST_ROWS_LIMIT = 256


class WidgetReadParams(Params):
    widget: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    params: dict[str, Any] = Field(default_factory=dict)


class WidgetRows(Params):
    rows: list[dict[str, Any]]
    source: str
    degraded: bool
    fetched_at: str


def _now() -> datetime:
    return datetime.now(UTC)


#: (tenant, widget) -> the rows of the last healthy read; softens a down connector only.
_last_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}


def _widgets() -> Registry:
    return get_registry()


def known_widgets() -> tuple[str, ...]:
    """Every widget name the ecosystem registry serves, ``services`` included."""
    return (SERVICES, *sorted(_widgets().list_all_known()))


def _remember(key: tuple[str, str], rows: list[dict[str, Any]]) -> None:
    if key not in _last_rows and len(_last_rows) >= _LAST_ROWS_LIMIT:
        _last_rows.pop(next(iter(_last_rows)))
    _last_rows[key] = rows


def _rows_of(data: WidgetData, labels: Mapping[str, str]) -> list[dict[str, Any]]:
    return [
        {"key": key, "label": labels.get(key, key), "value": value}
        for key, value in data.fields.items()
    ]


async def _services(context: Any) -> list[dict[str, Any]]:
    status = await invoke_registered(context, "fleet.status", {})
    value = status.get("value", status) if isinstance(status, Mapping) else {}
    children = value.get("children") if isinstance(value, Mapping) else None
    if not isinstance(children, list):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "fleet status has no children"}
        )
    return [dict(row) for row in children if isinstance(row, Mapping)]


async def _widget_rows(
    widget: str, fetch: Callable[[], WidgetData]
) -> list[dict[str, Any]]:
    loaded = _widgets().get_widget(widget)
    if loaded is None:
        raise OperationRefused("NOT_FOUND")
    data = await asyncio.to_thread(fetch)
    if data.status != "ok":
        raise ConnectionError(data.status)
    labels = {field.key: field.label for field in loaded.get_fields()}
    return _rows_of(data, labels)


async def handle_widget_read(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    widget = str(params["widget"])
    if widget not in known_widgets():
        raise OperationRefused("NOT_FOUND")
    key = (str(context.caller.tenant), widget)
    try:
        if widget == SERVICES:
            rows = await _services(context)
        else:
            config = ServiceConfig(id=widget, name=widget, widget_type=widget)
            loaded = _widgets().get_widget(widget)
            if loaded is None:
                raise OperationRefused("NOT_FOUND")
            rows = await _widget_rows(widget, lambda: loaded.fetch_data(config))
    except (ConnectionError, TimeoutError, OSError, RuntimeError):
        return {
            "rows": list(_last_rows.get(key, [])),
            "source": widget,
            "degraded": True,
            "fetched_at": _now().isoformat(),
        }
    _remember(key, rows)
    return {
        "rows": rows,
        "source": widget,
        "degraded": False,
        "fetched_at": _now().isoformat(),
    }


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="ecosystem.widgets.read",
            summary="Read one ecosystem widget as rows",
            examples=("read the services widget",),
            params=WidgetReadParams,
            result=WidgetRows,
            handler="arbora.api.ops.ecosystem.handle_widget_read",
            scope=SCOPE,
        ),
    )


specs = operations
