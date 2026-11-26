"""Source sync run reads, served refusing UNAVAILABLE (ARBORA-OPS-R922.3).

The connector control plane (``ARBORA-FLEET-R082``) owns run state and the engine
serves no run list for it yet. Arbora keeps no run record, so the read refuses
``UNAVAILABLE``. The shape the engine must serve is in the gaps list.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from arbora.api.ops._common import Params
from arbora.api.ops._pending import refuse_unavailable, unserved_read
from arbora.api.registry import OpSpec

handle_unserved = refuse_unavailable
SCOPE = "ingest:read"


class RunsListParams(Params):
    connection_id: str | None = Field(default=None, min_length=1, max_length=256)
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"] | None = (
        None
    )
    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


def operations() -> tuple[OpSpec, ...]:
    return (
        unserved_read(
            module=__name__,
            op_id="ingest.sources.runs.list",
            summary="List source sync runs",
            example="show the recent source sync runs",
            params=RunsListParams,
            scope=SCOPE,
        ),
    )


specs = operations

__all__ = ["handle_unserved", "operations", "specs"]
