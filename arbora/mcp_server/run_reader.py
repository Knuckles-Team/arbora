"""Read durable RunEvents after canonical WorkItem owner admission."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from medulla import EngineResponseError

from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.api.contracts import use_session
from arbora.api.errors import EngineRefusal
from arbora.api.invoke.bootstrap import _caller_dispatch_graph
from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.run_graph import (
    MAX_GRAPH_EVENTS,
    ExecutionGraphParams,
    declared_topology,
    project_graph,
    unavailable_graph,
)
from arbora.api.ops.run_replay import (
    MAX_HISTORY_EVENTS,
    MAX_PAGE_BYTES,
    SCHEMA,
    TERMINAL_KINDS,
    RunEventsParams,
    project_page,
    unavailable,
    validate_history,
)
from arbora.api.ops.runs import project_current_plan
from arbora.mcp_server.current_plan_reader import _project, read_native_snapshot

_RUN_STATUS = {
    "submitted": "pending",
    "ready": "pending",
    "leased": "running",
    "running": "running",
    "succeeded": "completed",
    "failed": "failed",
    "dead_letter": "failed",
    "cancelled": "cancelled",
}


class NativeRunReader:
    """Stateless host adapter; EG owns events and WorkItems, AU their ordering."""

    def __init__(self, engine_provider: Callable[[], Any]) -> None:
        self._authority = WorkItemA2AAuthority(engine_provider)

    async def _owned(
        self, context: ExecutionContext, run_id: str, task_id: str | None = None
    ) -> Any:
        await _caller_dispatch_graph(context)
        if "kg:read" not in context.caller.effective_scopes:
            raise OperationRefused("SCOPE_REQUIRED", {"missing_scopes": ["kg:read"]})
        with use_session(context.caller.session):
            try:
                root_task_id = task_id if task_id is not None else run_id
                item = await asyncio.to_thread(
                    self._authority._authorized_item, root_task_id, "kg:read"
                )
                if item is None:
                    return None
                metadata = item.get("metadata", {})
                if not isinstance(metadata, dict):
                    return None
                stored_run_id = metadata.get("a2a_run_id")
                if "a2a_run_id" in metadata:
                    return item if stored_run_id == run_id else None
                return item if root_task_id == run_id else None
            except EngineResponseError as exc:
                raise EngineRefusal(exc.code) from exc
            except ValueError:
                return None

    async def get(
        self, *, context: ExecutionContext, run_id: str, task_id: str | None = None
    ) -> dict[str, Any]:
        item = await self._owned(context, run_id, task_id)
        status = _RUN_STATUS.get(item.get("status")) if item is not None else None
        if status is None:
            return {
                "schema_version": SCHEMA,
                "availability": "unavailable",
                "run_id": run_id,
            }
        return {
            "schema_version": SCHEMA,
            "availability": "available",
            "run_id": run_id,
            "task_id": task_id if task_id is not None else run_id,
            "status": status,
            "task_plan": {"availability": "unavailable"},
            "public_summary": {"availability": "unavailable"},
        }

    async def events(
        self, *, context: ExecutionContext, request: RunEventsParams
    ) -> dict[str, Any]:
        item = await self._owned(context, request.run_id, request.task_id)
        if item is None:
            return unavailable(request.run_id)
        loaded = await self._page(context, request)
        return unavailable(request.run_id) if loaded is None else loaded[1]

    async def execution_graph(
        self, *, context: ExecutionContext, request: ExecutionGraphParams
    ) -> dict[str, Any]:
        """One caller-bound native plan and bounded prefix, with no local store."""
        if await self._owned(context, request.run_id, request.task_id) is None:
            return unavailable_graph(request)
        first = await read_native_snapshot(context, request.run_id)
        topology = _current_topology(first, request.run_id)
        if topology is None:
            return unavailable_graph(request)
        replay = RunEventsParams(
            run_id=request.run_id, task_id=request.task_id, limit=MAX_GRAPH_EVENTS
        )
        loaded = await self._page(context, replay)
        if loaded is None or loaded[1]["availability"] != "available":
            return unavailable_graph(request)
        # The owner and native publication can change while history is collected.
        if await self._owned(context, request.run_id, request.task_id) is None:
            return unavailable_graph(request)
        last = await read_native_snapshot(context, request.run_id)
        if (
            _current_topology(last, request.run_id) != topology
            or last["task_plan"] != first["task_plan"]
        ):
            return unavailable_graph(request)
        try:
            return project_graph(request, topology, *loaded)
        except (ValueError, TypeError, KeyError, UnicodeError):
            return unavailable_graph(request)

    async def _page(
        self, context: ExecutionContext, request: RunEventsParams
    ) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
        with use_session(context.caller.session):
            try:
                engine, _ = self._authority._engine()
                history = validate_history(
                    await asyncio.to_thread(
                        engine.query_cypher,
                        "MATCH (e) WHERE e.type = 'RunEvent' AND e.run_id = $run_id "
                        "RETURN e.sequence AS sequence, e.event_id AS event_id, "
                        "e.event_type AS event_type ORDER BY e.sequence LIMIT $limit",
                        {"run_id": request.run_id, "limit": MAX_HISTORY_EVENTS + 1},
                    )
                )
                if history is None or request.after > len(history):
                    return None
                rows = await _hydrate_page(engine, request, len(history))
                if rows is None:
                    return None
            except EngineResponseError as exc:
                raise EngineRefusal(exc.code) from exc
        return rows, _reconcile_page(rows, history, request)


def _current_topology(raw: Any, run_id: str) -> tuple[str, dict[str, list[str]]] | None:
    current = project_current_plan(_project(raw), run_id=run_id, now=datetime.now(UTC))
    if current.availability != "available":
        return None
    try:
        return declared_topology(raw)
    except (ValueError, TypeError, KeyError):
        return None


async def _hydrate_page(
    engine: Any, request: RunEventsParams, history_size: int
) -> list[dict[str, Any]] | None:
    rows: list[dict[str, Any]] = []
    raw_bytes = 0
    remaining = min(request.limit + 1, history_size - request.after)
    while remaining:
        batch_size = min(16, remaining)
        batch = await asyncio.to_thread(
            engine.query_cypher,
            "MATCH (e) WHERE e.type = 'RunEvent' AND e.run_id = $run_id "
            "AND e.sequence >= $first RETURN e.payload AS payload "
            "ORDER BY e.sequence LIMIT $limit",
            {
                "run_id": request.run_id,
                "first": request.after + len(rows) + 1,
                "limit": batch_size,
            },
        )
        if not isinstance(batch, list) or len(batch) != batch_size:
            return None
        batch_bytes = _batch_payload_size(batch, raw_bytes)
        if batch_bytes is None:
            return None
        raw_bytes = batch_bytes
        rows.extend(batch)
        remaining -= len(batch)
    return rows


def _batch_payload_size(batch: list[Any], size: int) -> int | None:
    for row in batch:
        payload = row.get("payload") if isinstance(row, dict) else None
        if not isinstance(payload, str):
            return None
        size += len(payload.encode())
        if size > MAX_PAGE_BYTES:
            return None
    return size


def _reconcile_page(
    rows: list[dict[str, Any]], history: list[dict[str, Any]], request: RunEventsParams
) -> dict[str, Any]:
    page = project_page(rows, request)
    if page["availability"] != "available":
        return page
    expected = history[request.after : request.after + request.limit + 1]
    # Reject racing or inconsistent projections rather than claiming a cursor.
    if len(rows) != len(expected):
        return unavailable(request.run_id)
    for event, metadata in zip(page["events"], expected, strict=False):
        if (
            event["event_id"] != metadata["event_id"]
            or event["type"] != metadata["event_type"]
        ):
            return unavailable(request.run_id)
    page["terminal"] = page["terminal"] or bool(
        request.after and history[request.after - 1]["event_type"] in TERMINAL_KINDS
    )
    return page
