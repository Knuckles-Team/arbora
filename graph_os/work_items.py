"""Graph OS seam for durable WorkItems.

The engine serves the WorkItem methods (``client.work_item``: submit, claim,
renew, commit_result, defer, cancel, get). Graph OS surfaces reached them
through an in-process wrapper that agent-utilities no longer ships. Until each
caller binds the engine methods directly, every verb here refuses with the
owning spec row, so no surface fabricates a durable transition.
"""

from __future__ import annotations

from typing import Any, NoReturn

from graph_os.api.errors import not_served

#: The engine's spec row that owns durable WorkItem transitions.
SPEC_ROW = "EG-INGEST-WORKER-R605"

#: Statuses after which a WorkItem never transitions again.
TERMINAL_WORK_ITEM_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "dead_letter"}
)


class WorkItemBackendUnavailable(RuntimeError):
    """The engine WorkItem authority cannot serve the transition."""


class NativeWorkItemRequired(WorkItemBackendUnavailable):
    """The engine-native WorkItem method is required and not bound."""


def _refuse() -> NoReturn:
    raise not_served("knowledge_graph.core.work_durability", SPEC_ROW)


def submit_work_item_atomic(*_args: Any, **_kwargs: Any) -> tuple[str, bool]:
    """Submit a WorkItem through the engine ``work_item.submit`` method."""
    _refuse()


def get_work_item(*_args: Any, **_kwargs: Any) -> dict[str, Any] | None:
    """Read a WorkItem through the engine ``work_item.get`` method."""
    _refuse()


def claim_specific(*_args: Any, **_kwargs: Any) -> dict[str, Any] | None:
    """Claim one WorkItem through the engine ``work_item.claim`` method."""
    _refuse()


def heartbeat(*_args: Any, **_kwargs: Any) -> bool:
    """Renew a WorkItem lease through the engine ``work_item.renew`` method."""
    _refuse()


def commit_result(*_args: Any, **_kwargs: Any) -> bool:
    """Commit an outcome through the engine ``work_item.commit_result`` method."""
    _refuse()


def defer_work_item(*_args: Any, **_kwargs: Any) -> bool:
    """Defer a WorkItem through the engine ``work_item.defer`` method."""
    _refuse()


def cancel_work_item(*_args: Any, **_kwargs: Any) -> bool:
    """Cancel a WorkItem through the engine ``work_item.cancel`` method."""
    _refuse()


__all__ = [
    "SPEC_ROW",
    "TERMINAL_WORK_ITEM_STATUSES",
    "NativeWorkItemRequired",
    "WorkItemBackendUnavailable",
    "cancel_work_item",
    "claim_specific",
    "commit_result",
    "defer_work_item",
    "get_work_item",
    "heartbeat",
    "submit_work_item_atomic",
]
