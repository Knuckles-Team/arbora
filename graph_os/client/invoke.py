"""Generic typed GraphOS operation invoker. Generated; do not edit."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from ._generated_models import REGISTRY_DIGEST, OperationId, schema_for


class Transport(Protocol):
    async def invoke(
        self, op_id: str, params: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


async def invoke(
    transport: Transport, op_id: OperationId, params: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Use one transport entrypoint for every declared operation."""
    schema_for(op_id)
    response = await transport.invoke(op_id, params)
    meta = response.get("meta")
    if not isinstance(meta, Mapping) or meta.get("registry_digest") != REGISTRY_DIGEST:
        raise RuntimeError("GraphOS registry digest mismatch")
    return response
