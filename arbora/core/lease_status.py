"""The closed status vocabulary of one engine lease (grants and elevations)."""

from __future__ import annotations

from enum import StrEnum


class LeaseStatus(StrEnum):
    """Where an engine lease is: live, spent, ended early, or timed out."""

    ACTIVE = "active"
    CONSUMED = "consumed"
    REVOKED = "revoked"
    EXPIRED = "expired"
