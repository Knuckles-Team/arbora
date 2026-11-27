"""Errors of the engine WorkItem authority that more than one unit raises or reads."""

from __future__ import annotations

__all__ = ["NativeWorkItemRequired", "WorkItemBackendUnavailable"]


class WorkItemBackendUnavailable(RuntimeError):
    """The engine WorkItem authority cannot serve the transition."""


class NativeWorkItemRequired(WorkItemBackendUnavailable):
    """The engine-native WorkItem method is required and not bound."""
