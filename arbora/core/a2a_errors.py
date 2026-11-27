"""Errors of the A2A task authority that more than one unit raises or reads."""

from __future__ import annotations

__all__ = ["A2AIdempotencyConflict"]


class A2AIdempotencyConflict(RuntimeError):
    """An idempotency key was replayed with a different logical request."""
