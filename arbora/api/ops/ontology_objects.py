"""Ontology interface reads, served refusing UNAVAILABLE (ARBORA-OPS-R922.1).

The engine ontology schema carries no interface marker yet, and arbora invents
none, so both reads refuse ``UNAVAILABLE``. The shape the engine must serve is in
the gaps list.
"""

from __future__ import annotations

from pydantic import Field

from arbora.api.ops._common import Params
from arbora.api.ops._pending import refuse_unavailable, unserved_read
from arbora.api.registry import OpSpec

handle_unserved = refuse_unavailable
SCOPE = "kg:read"


class InterfacesListParams(Params):
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


class ImplementersParams(InterfacesListParams):
    name: str = Field(min_length=1, max_length=256)


def operations() -> tuple[OpSpec, ...]:
    m = __name__
    return (
        unserved_read(
            module=m,
            op_id="ontology.interfaces.list",
            summary="List the ontology interfaces",
            example="which ontology interfaces exist",
            params=InterfacesListParams,
            scope=SCOPE,
        ),
        unserved_read(
            module=m,
            op_id="ontology.interfaces.implementers",
            summary="List the classes that implement one interface",
            example="which classes implement this interface",
            params=ImplementersParams,
            scope=SCOPE,
        ),
    )


specs = operations

__all__ = ["handle_unserved", "operations", "specs"]
