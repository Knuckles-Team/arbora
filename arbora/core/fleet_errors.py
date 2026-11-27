"""Errors of the fleet catalog that more than one unit raises or reads."""

from __future__ import annotations

__all__ = ["FleetServerNotFound"]


class FleetServerNotFound(LookupError):
    """A per-server catalog read for a server the snapshot holds nothing for.

    Carries the server's pack status so the caller answers ``NOT_FOUND``
    with it, never an empty catalog (ARBORA-FLEET-R040.3)."""

    def __init__(self, server: str, pack_status: str) -> None:
        super().__init__(server)
        self.server = server
        self.pack_status = pack_status
