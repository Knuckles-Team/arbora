"""The closed status vocabulary of one durable WorkItem.

The engine owns the WorkItem lifecycle (``MEDULLA-INGEST-WORKER-R605``); this is
the same eight stored status texts, typed so a status is never a free string.
"""

from __future__ import annotations

from enum import StrEnum


class WorkItemStatus(StrEnum):
    """Where a WorkItem is in the engine's lifecycle."""

    SUBMITTED = "submitted"
    READY = "ready"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTER = "dead_letter"
