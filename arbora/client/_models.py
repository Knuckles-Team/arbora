"""Operation IDs and schema lookup, read from the in-code registry (the source of truth)."""

from __future__ import annotations

import json
from functools import lru_cache

type OperationId = str


@lru_cache(maxsize=1)
def _registry() -> tuple[str, dict[str, dict]]:
    from arbora.api.ops.registry_factory import get_registry

    registry = get_registry()
    document = json.loads(registry.canonical)
    return registry.digest, {op["id"]: op for op in document["ops"]}


def registry_digest() -> str:
    """The digest a server built from the same registry stamps on every response."""
    return _registry()[0]


def schema_for(op_id: OperationId) -> dict:
    op = _registry()[1][op_id]
    return {field: op[field] for field in ("params", "result")}
