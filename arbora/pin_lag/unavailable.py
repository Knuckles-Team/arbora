"""The one refusal of a capability the pinned release lacks (PIN-LAG).

Every guard in this package raises this class, so a caller catches one type and
reads ``missing`` for the exact method, shape or symbol the pinned release lacks.
"""

from __future__ import annotations


class PinLagUnavailable(RuntimeError):
    """The pinned upstream lacks a method, shape or symbol; ``missing`` names it."""

    code = "UNAVAILABLE"

    def __init__(self, missing: str) -> None:
        self.missing = missing
        super().__init__(f"UNAVAILABLE: the pinned release lacks {missing}")
