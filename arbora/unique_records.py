"""Create a record whose node id is unique: EG ``CreateNodeIfAbsent``.

A decision, an approval, a run event, a fleet event, an error detail, a
runtime signal, a trace checkpoint, a configuration change and a mirrored
message are each written once under an id that never names another record.
That write is a create, not an upsert. The served engine sends it as EG
``CreateNodeIfAbsent``, which is idempotent by id; ``AddNode`` is kept for
records that are updated in place (an inbox row's status, a channel
preference).

On the live 0.1.5 engine ``AddNode`` on the session graph was refused
(``INTERNAL: unclassified engine refusal``) while ``CreateNodeIfAbsent`` on
the same graph was admitted, so a policy decision write failed every pack
import. Every unique-record writer goes through :func:`create_record`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["create_record", "has_record_writer"]


def has_record_writer(engine: Any) -> bool:
    """Whether ``engine`` exposes the required atomic create operation."""
    return engine is not None and callable(getattr(engine, "create_node", None))


def create_record(
    engine: Any,
    node_id: str,
    label: str,
    properties: Mapping[str, Any] | None = None,
) -> bool:
    """Create one ``label`` node ``node_id`` unless it already exists.

    Uses the engine's atomic ``create_node`` (EG ``CreateNodeIfAbsent``).
    An engine without this operation is refused without any write.

    Returns:
        True when this call created the node; False when it already existed.

    Raises:
        RuntimeError: ``engine`` has no atomic create operation.
        Exception: the engine's refusal, unchanged.
    """
    props = dict(properties or {})
    create = getattr(engine, "create_node", None) if engine is not None else None
    if callable(create):
        return bool(create(node_id, label, props))
    raise RuntimeError("no engine atomic create operation is bound")
