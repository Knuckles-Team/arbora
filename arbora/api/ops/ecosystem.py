"""Ecosystem widget reads (ARBORA-OPS-R914).

``ecosystem.widgets.read`` projects one connector as rows. There is no widget
table here: the mapping from a widget to the tool that feeds it is connector
pack data. A widget is one connector of the fleet census (its name with ``-``
read as ``_``); the tool it projects is the connector's read-only tool that
needs no argument, chosen from the catalog the pack imported (a tool whose pack
hints carry ``widget`` wins, otherwise the first by name). The call goes through
``fleet.call`` as the caller, so scope, effect gate and audit are the fleet's.

A connector that is down never fails the call: the reply is ``degraded`` and
carries the last rows read for that tenant and widget, or none. The cache only
softens a down connector; it is never the source of a healthy answer.
``services`` is read from the fleet status ``children``, not from ``servers``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops.nested_invoke import invoke_registered
from arbora.api.registry import OpSpec

SCOPE = "fleet:read"
SERVICES = "services"
_LAST_ROWS_LIMIT = 256
_CENSUS_PAGES = 20
_ROW_LIMIT = 500
_LIST_KEYS = ("rows", "items", "results", "data")


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


class LastRows:
    """(tenant, widget) -> the rows of the last healthy read; softens a down connector only.

    One instance is the host's, bound as the ``widget_rows`` service.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def remember(self, key: tuple[str, str], rows: list[dict[str, Any]]) -> None:
        if key not in self._rows and len(self._rows) >= _LAST_ROWS_LIMIT:
            self._rows.pop(next(iter(self._rows)))
        self._rows[key] = rows

    def get(self, key: tuple[str, str]) -> list[dict[str, Any]]:
        return list(self._rows.get(key, []))


def _last_rows(context: Any) -> LastRows:
    return _common.bound_service(
        context, "widget_rows", reason="the widget row cache is not composed"
    )


def widget_name(server: str) -> str:
    """The widget name of a connector: its fleet name with ``-`` read as ``_``."""
    return server.replace("-", "_")


def _unwrap(reply: Any) -> Any:
    return reply.get("value", reply) if isinstance(reply, Mapping) else reply


async def _services(context: Any) -> list[dict[str, Any]]:
    value = _unwrap(await invoke_registered(context, "fleet.status", {}))
    children = value.get("children") if isinstance(value, Mapping) else None
    if isinstance(children, Mapping):
        return [
            {"name": name, **dict(child)}
            for name, child in sorted(children.items())
            if isinstance(child, Mapping)
        ]
    if isinstance(children, list):
        return [dict(row) for row in children if isinstance(row, Mapping)]
    raise OperationRefused("UNAVAILABLE", {"reason": "fleet status has no children"})


async def connector_servers(context: Any) -> dict[str, str]:
    """Widget name -> fleet server for every connector of the fleet census."""
    servers: dict[str, str] = {}
    cursor: str | None = None
    for _page in range(_CENSUS_PAGES):
        arguments: dict[str, Any] = {"summary": True, "limit": 100}
        if cursor:
            arguments["cursor"] = cursor
        value = _unwrap(
            await invoke_registered(context, "fleet.catalog.list", arguments)
        )
        if not isinstance(value, Mapping):
            break
        for row in value.get("servers", ()):
            if isinstance(row, Mapping) and isinstance(row.get("server"), str):
                servers[widget_name(row["server"])] = row["server"]
        cursor = value.get("next_cursor")
        if not cursor:
            break
    return servers


def _hint(annotations: Any, *names: str) -> bool:
    return isinstance(annotations, Mapping) and any(
        annotations.get(name) is True for name in names
    )


def _needs_no_argument(row: Mapping[str, Any]) -> bool:
    schema = row.get("input_schema")
    return not (isinstance(schema, Mapping) and schema.get("required"))


async def widget_tool(context: Any, server: str) -> str:
    """The connector tool its widget projects, from the imported pack's catalog."""
    gateway = context.services.get("fleet_gateway")
    if gateway is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "fleet gateway is not bound"})
    value = _unwrap(
        await invoke_registered(
            context,
            "fleet.catalog.list",
            {"kinds": ["tool"], "servers": [server], "browse": True, "limit": 100},
        )
    )
    rows = value.get("items", ()) if isinstance(value, Mapping) else ()
    chosen: list[tuple[bool, str]] = []
    for row in rows:
        if not isinstance(row, Mapping) or not _needs_no_argument(row):
            continue
        item = await gateway.catalog.get(str(row.get("id")), context.caller)
        hints = getattr(item, "annotations", None)
        if _hint(hints, "readOnlyHint", "read_only_hint"):
            chosen.append((not _hint(hints, "widget"), str(row["name"])))
    if not chosen:
        raise OperationRefused("NOT_FOUND")
    return sorted(chosen)[0][1]


def _decoded(value: Any) -> Any:
    """The structured payload of a connector result, whatever envelope carries it."""
    if isinstance(value, Mapping):
        for name in ("structuredContent", "structured_content"):
            if value.get(name) is not None:
                return value[name]
        content = value.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            text = first.get("text") if isinstance(first, Mapping) else None
            if isinstance(text, str):
                try:
                    return json.loads(text)
                except ValueError:
                    return {"text": text}
    return value


def project_rows(value: Any) -> list[dict[str, Any]]:
    """Rows from a connector result: a list of records, or a record as key/value rows."""
    payload = _decoded(value)
    if isinstance(payload, Mapping):
        for name in _LIST_KEYS:
            if isinstance(payload.get(name), list):
                payload = payload[name]
                break
    if isinstance(payload, list):
        return [
            dict(row) if isinstance(row, Mapping) else {"value": row}
            for row in payload[:_ROW_LIMIT]
        ]
    if isinstance(payload, Mapping):
        return [
            {"key": key, "label": key, "value": item}
            for key, item in list(payload.items())[:_ROW_LIMIT]
        ]
    return [{"value": payload}] if payload is not None else []


def _failed(value: Any) -> bool:
    return isinstance(value, Mapping) and (
        set(value) == {"error"} or value.get("isError") is True
    )


async def handle_widget_read(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    widget = str(params["widget"])
    key = (str(context.caller.tenant), widget)
    try:
        if widget == SERVICES:
            rows = await _services(context)
        else:
            servers = await connector_servers(context)
            server = servers.get(widget)
            if server is None:
                raise OperationRefused("NOT_FOUND")
            tool = await widget_tool(context, server)
            reply = _unwrap(
                await invoke_registered(
                    context,
                    "fleet.call",
                    {
                        "server": server,
                        "tool": tool,
                        "arguments": dict(params.get("params") or {}),
                    },
                )
            )
            if _failed(reply):
                raise ConnectionError(widget)
            rows = project_rows(reply)
    except OperationRefused as refusal:
        if refusal.code != "UNAVAILABLE":
            raise
        return _degraded(context, key, widget)
    except (ConnectionError, TimeoutError, OSError, RuntimeError):
        return _degraded(context, key, widget)
    _last_rows(context).remember(key, rows)
    return {
        "rows": rows,
        "source": widget,
        "degraded": False,
        "fetched_at": _now().isoformat(),
    }


def _degraded(context: Any, key: tuple[str, str], widget: str) -> dict[str, Any]:
    return {
        "rows": _last_rows(context).get(key),
        "source": widget,
        "degraded": True,
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
