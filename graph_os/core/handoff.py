"""Refusal for capabilities that moved out of Graph OS.

A served operation whose capability now lives in another component refuses
with :class:`CapabilityMovedError`. The error names the operation, the source
capability and its owner, so a caller can route the request to that owner.
"""

from __future__ import annotations

from typing import NoReturn

__all__ = ["CapabilityMovedError", "raise_moved"]


class CapabilityMovedError(RuntimeError):
    """The requested capability is served by another component."""

    def __init__(self, operation: str, capability: str, owner: str) -> None:
        super().__init__(
            f"{operation}: the {capability} capability moved to {owner}; "
            "Graph OS does not run it"
        )
        self.operation = operation
        self.capability = capability
        self.owner = owner


def raise_moved(operation: str, capability: str, owner: str) -> NoReturn:
    """Refuse ``operation`` because ``capability`` is now served by ``owner``."""
    raise CapabilityMovedError(operation, capability, owner)
