"""Persistence privacy masking on the served write path (ARBORA-OPS-R848).

Every operation that stores caller-supplied content passes the same
``sanitize_for_persistence`` policy before the effect is reserved, so a
secret-bearing payload is masked no matter which surface (web UI, MCP, A2A)
sent it. The table names the persisted field of each such operation; the
masked arguments are what the idempotency digest, the audit and the engine see.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import msgpack
from agent_connector_sdk.privacy import sanitize_for_persistence

#: Operation id -> (argument name, wire encoding of that argument).
PERSISTED_FIELDS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "memory.write": ("value", "json"),
        "eg.graph.AddEdge": ("properties_msgpack", "msgpack"),
        # ARBORA-OPS-R844.2: node writes and mutation batches from the web UI.
        "eg.graph.AddNode": ("properties_msgpack", "msgpack"),
        "eg.graph.ApplyMutation": ("query", "json"),
    }
)


def mask_persisted_arguments(
    op_id: str, arguments: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Return ``arguments`` with the persisted field masked.

    ``None`` means the persisted field could not be decoded for masking, so the
    call is refused rather than stored unmasked.
    """
    declared = PERSISTED_FIELDS.get(op_id)
    masked = dict(arguments)
    if declared is None:
        return masked
    name, encoding = declared
    if name not in masked or masked[name] is None:
        return masked
    if encoding == "json":
        masked[name], _report = sanitize_for_persistence(masked[name])
        return masked
    try:
        decoded = msgpack.unpackb(bytes(masked[name]), raw=False)
    except (ValueError, TypeError, msgpack.exceptions.UnpackException):
        return None
    clean, _report = sanitize_for_persistence(decoded)
    masked[name] = list(msgpack.packb(clean))
    return masked
