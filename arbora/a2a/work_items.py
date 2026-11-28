"""Typed projection of a durable WorkItem record (ARBORA-FLEET-R005.1).

Producer slice for ARBORA-FLEET-R005: before ``GetWorkItem``/``ListWorkItems``
can route inbound A2A tasks through a typed operation, the durable work-item
record each of them reads needs a typed, validated shape instead of a bare
``dict``. This module defines that shape and projects it from the same
record fields ``WorkItemA2AAuthority`` already writes
(``kind``, ``tenant``, ``created_by``, ``metadata``, and the work-item id
under the ``workitem:orchestrator:`` namespace) — it adds no new persistence,
dispatch, capability-search, or lease behavior.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from arbora.core.work_item_status import WorkItemStatus

__all__ = [
    "WorkItemView",
    "parse_work_item_view",
    "work_item_view",
    "list_work_item_views",
]

_REQUIRED_STRING_FIELDS: tuple[str, ...] = (
    "work_item_id",
    "kind",
    "tenant",
    "created_by",
)


@dataclass(frozen=True, slots=True)
class WorkItemView:
    """A validated, read-only projection of one durable WorkItem record."""

    work_item_id: str
    kind: str
    tenant: str
    created_by: str
    queue: str | None
    state: WorkItemStatus | None
    metadata: Mapping[str, Any]


def _missing_or_blank(record: Mapping[str, Any], field_name: str) -> bool:
    value = record.get(field_name)
    return not isinstance(value, str) or not value.strip()


def _known_state(value: object) -> WorkItemStatus | None:
    """The engine's status of a record, or ``None`` when it is absent or not one."""
    try:
        return WorkItemStatus(value) if isinstance(value, str) else None
    except ValueError:
        return None


def work_item_view(record: Mapping[str, Any]) -> WorkItemView:
    """Project one durable WorkItem record, refusing a malformed record.

    A record missing, or blanking, any of its required identity fields is
    not a real work item to serve through ``GetWorkItem`` — refuse it
    instead of returning a partially typed view a caller could mistake for
    a complete one.
    """
    for field_name in _REQUIRED_STRING_FIELDS:
        if _missing_or_blank(record, field_name):
            raise ValueError(f"work item record is missing {field_name!r}")
    metadata = record.get("metadata")
    if metadata is not None and not isinstance(metadata, Mapping):
        raise ValueError("work item metadata must be a mapping when present")
    queue = record.get("queue")
    state = record.get("state")
    return WorkItemView(
        work_item_id=record["work_item_id"],
        kind=record["kind"],
        tenant=record["tenant"],
        created_by=record["created_by"],
        queue=queue if isinstance(queue, str) else None,
        state=_known_state(state),
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )


def parse_work_item_view(record: Mapping[str, Any]) -> WorkItemView | None:
    """Like ``work_item_view``, but reports a malformed record as absent."""
    try:
        return work_item_view(record)
    except ValueError:
        return None


def list_work_item_views(
    records: Iterable[Mapping[str, Any]],
) -> tuple[WorkItemView, ...]:
    """Project every parseable record for ``ListWorkItems``, in source order.

    A malformed record is dropped rather than failing the whole listing —
    the same fail-safe-for-reads posture ``_probed_item`` already uses for
    one bad catalog row.
    """
    return tuple(
        view for view in (parse_work_item_view(record) for record in records) if view
    )
