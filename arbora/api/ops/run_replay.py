"""Public, bounded projections of durable AU execution events."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field, field_validator

from arbora.api.ops._common import Params

SCHEMA: Literal["arbora-run-events-v1"] = "arbora-run-events-v1"
MAX_PAGE_BYTES = 1_048_576
MAX_HISTORY_EVENTS = 10_000
EVENT_KINDS = frozenset(
    {
        "run_started",
        "node_transition",
        "graph_complete",
        "error",
        "run_cancelled",
        "task_started",
        "task_completed",
        "task_failed",
        "task_cancelled",
        "task_blocked",
        "public_summary",
    }
)
TERMINAL_KINDS = frozenset({"graph_complete", "error", "run_cancelled"})


class RunReadParams(Params):
    run_id: str = Field(min_length=1, max_length=512, strict=True)
    task_id: str | None = Field(default=None, min_length=1, max_length=512, strict=True)


class RunEventsParams(RunReadParams):
    after: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    limit: int = Field(default=1000, ge=1, le=1000, strict=True)

    @field_validator("after", "limit", mode="before")
    @classmethod
    def query_integer(cls, value: Any) -> Any:
        if (
            isinstance(value, str)
            and value.isascii()
            and value.isdecimal()
            and len(value) <= 20
        ):
            return int(value)
        return value


class PublicRunEvent(Params):
    schema_version: Literal["arbora-run-events-v1"] = SCHEMA
    event_id: str
    sequence: int
    run_id: str
    task_id: str
    type: str
    category: Literal["execution_trace", "public_summary"]
    terminal: bool
    payload: dict[str, Any]


def unavailable(run_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA,
        "availability": "unavailable",
        "run_id": run_id,
        "events": [],
        "next_after": None,
        "has_more": False,
        "terminal": False,
    }


def project_event(raw: Any, *, run_id: str) -> dict[str, Any]:
    """Allowlist public facts only; never expose an opaque producer payload."""
    if not isinstance(raw, dict) or raw.get("run_id") != run_id:
        raise ValueError("invalid run event")
    sequence, event_id, kind, task_id = _event_identity(raw, run_id)
    payload = _public_payload(raw)
    return PublicRunEvent(
        event_id=event_id,
        sequence=sequence,
        run_id=run_id,
        task_id=task_id,
        type=kind,
        category="public_summary" if kind == "public_summary" else "execution_trace",
        terminal=kind in TERMINAL_KINDS,
        payload=payload,
    ).model_dump(mode="json")


def _event_identity(raw: dict[str, Any], run_id: str) -> tuple[int, str, str, str]:
    sequence = raw.get("sequence")
    event_id = raw.get("event_id")
    kind = raw.get("type")
    if (
        type(sequence) is not int
        or not 0 < sequence <= 2**63 - 1
        or not isinstance(event_id, str)
        or not 0 < len(event_id) <= 1024
        or kind not in EVENT_KINDS
        or "payload_error" in raw
    ):
        raise ValueError("unavailable ordered event")
    task_id = raw.get("task_id", run_id)
    if not isinstance(task_id, str) or not 0 < len(task_id) <= 512:
        raise ValueError("invalid task identity")
    return sequence, event_id, kind, task_id


def _public_payload(raw: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in ("status", "node", "from_node", "to_node", "error_type"):
        value = raw.get(key)
        if isinstance(value, str) and len(value) <= 256:
            payload[key] = value
    step = raw.get("step")
    if type(step) is int and 0 <= step <= 2**63 - 1:
        payload["step"] = step
    _project_dependencies(raw, payload)
    if raw.get("type") == "public_summary":
        summary = raw.get("summary")
        if not isinstance(summary, str) or not 0 < len(summary) <= 16_384:
            raise ValueError("invalid public summary")
        payload["summary"] = summary
    return payload


def _project_dependencies(raw: dict[str, Any], payload: dict[str, Any]) -> None:
    dependencies = raw.get("depends_on")
    if (
        isinstance(dependencies, list)
        and len(dependencies) <= 64
        and all(isinstance(item, str) and 0 < len(item) <= 512 for item in dependencies)
    ):
        payload["depends_on"] = dependencies


def project_page(rows: Any, request: RunEventsParams) -> dict[str, Any]:
    """Validate ordered durable rows; gaps and malformed history fail closed."""
    if not isinstance(rows, list) or len(rows) > request.limit + 1:
        return unavailable(request.run_id)
    try:
        events = _ordered_page(rows, request)
    except (ValueError, TypeError, UnicodeError):
        return unavailable(request.run_id)
    visible = events[: request.limit]
    return {
        "schema_version": SCHEMA,
        "availability": "available",
        "run_id": request.run_id,
        "after": request.after,
        "events": visible,
        "next_after": visible[-1]["sequence"] if visible else request.after,
        "has_more": len(events) > request.limit,
        "terminal": bool(visible and visible[-1]["terminal"]),
    }


def _decode_event_row(row: Any, run_id: str) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row, dict) else None
    if not isinstance(payload, str) or len(payload.encode()) > MAX_PAGE_BYTES:
        raise ValueError("event payload unavailable")
    return project_event(json.loads(payload), run_id=run_id)


def _ordered_page(rows: list[Any], request: RunEventsParams) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    size = 0
    event_ids: set[str] = set()
    for index, row in enumerate(rows):
        event = _decode_event_row(row, request.run_id)
        if event["sequence"] != request.after + index + 1:
            raise ValueError("event history gap")
        if event["event_id"] in event_ids:
            raise ValueError("duplicate event identity")
        event_ids.add(event["event_id"])
        if events and events[-1]["terminal"]:
            raise ValueError("event follows terminal")
        size += len(json.dumps(event).encode())
        if size > MAX_PAGE_BYTES:
            raise ValueError("event page exceeds bound")
        events.append(event)
    return events


def validate_history(rows: Any) -> list[dict[str, Any]] | None:
    """Bounded metadata-only census: a cursor cannot hide corrupt history."""
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_HISTORY_EVENTS:
        return None
    seen: set[str] = set()
    terminal = False
    for sequence, row in enumerate(rows, start=1):
        if terminal or not _valid_history_row(row, sequence, seen):
            return None
        identity, kind = row["event_id"], row["event_type"]
        seen.add(identity)
        terminal = kind in TERMINAL_KINDS
    return rows


def _valid_history_row(row: Any, sequence: int, seen: set[str]) -> bool:
    if not isinstance(row, dict):
        return False
    identity, kind = row.get("event_id"), row.get("event_type")
    return (
        type(row.get("sequence")) is int
        and row["sequence"] == sequence
        and isinstance(identity, str)
        and 0 < len(identity) <= 1024
        and identity not in seen
        and isinstance(kind, str)
        and kind in EVENT_KINDS
    )
