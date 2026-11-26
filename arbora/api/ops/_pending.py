"""The handler of an operation that waits on a pending engine scope."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arbora.api.ops._engine import unavailable
from arbora.api.registry import OpSpec


async def refuse_unavailable(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Refuse without calling the engine: its pinned catalog lacks the scope."""
    raise unavailable("the pinned engine does not serve this scope yet")
