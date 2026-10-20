"""Read-only terminal replay from EG's native committed-event authority."""

from __future__ import annotations

import re
from typing import Any

from .terminal_event import A2ATerminalEventUnavailable, project_terminal_event

_CURSOR = re.compile(r"eg-terminal-v1:[0-9a-f]{64}")


def project_replay(
    value: Any,
    *,
    session: Any,
    task_id: str,
    owner_ref: str,
    work_item_id: str,
    run_id: str,
    after_cursor: str | None,
) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
    """Validate ownership even for an empty replay; never manufacture an event."""
    if not isinstance(value, dict):
        raise A2ATerminalEventUnavailable("terminal replay is unavailable")
    item = value.get("work_item")
    events = value.get("events")
    cursor = value.get("next_cursor")
    metadata = item.get("metadata") if isinstance(item, dict) else None
    if not (
        isinstance(item, dict)
        and item.get("work_item_id") == work_item_id
        and item.get("kind") == "orchestrator_task"
        and isinstance(metadata, dict)
        and metadata.get("a2a_owner_ref") == owner_ref
        and metadata.get("a2a_schema") == "arbora-a2a-unary-v1"
        and metadata.get("a2a_run_id", task_id) == run_id
        and item.get("status") in {"succeeded", "failed", "cancelled", "dead_letter"}
        and isinstance(cursor, str)
        and _CURSOR.fullmatch(cursor)
        and isinstance(events, list)
        and len(events) <= 1
    ):
        raise A2ATerminalEventUnavailable("terminal replay is unavailable")
    if not events:
        if after_cursor != cursor:
            raise A2ATerminalEventUnavailable("terminal replay is unavailable")
        return item, None, cursor
    raw = events[0]
    if (
        not isinstance(raw, dict)
        or raw.get("work_item_id") != work_item_id
        or raw.get("replay_cursor") != cursor
        or after_cursor is not None
    ):
        raise A2ATerminalEventUnavailable("terminal replay is unavailable")
    event = {
        key: val
        for key, val in raw.items()
        if key not in {"work_item_id", "replay_cursor"}
    }
    projected = project_terminal_event(
        {"work_item": item, "terminal_event": event},
        session=session,
        task_id=task_id,
        owner_ref=owner_ref,
        work_item_id=work_item_id,
        run_id=run_id,
    )
    if projected["status"] != "available":
        raise A2ATerminalEventUnavailable("terminal replay is unavailable")
    return (
        item,
        {**projected, "work_item_id": work_item_id, "replay_cursor": cursor},
        cursor,
    )
