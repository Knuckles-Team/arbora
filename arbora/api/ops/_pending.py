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


def unserved_read(
    *,
    module: str,
    op_id: str,
    summary: str,
    example: str,
    params: type[Any],
    scope: str,
) -> OpSpec:
    """A read the engine does not serve yet, bound to the refusal handler.

    The handler is the module's own alias of :func:`refuse_unavailable`; the
    operation stays registered so its refusal is testable and discoverable.
    """
    from arbora.api.ops._common import build_read_op
    from arbora.api.ops.knowledge import EngineReply

    return build_read_op(
        op_id=op_id,
        summary=summary,
        examples=(example,),
        params=params,
        result=EngineReply,
        handler=f"{module}.handle_unserved",
        scope=scope,
    )


def unserved_write(
    *,
    module: str,
    op_id: str,
    summary: str,
    example: str,
    params: type[Any],
    scope: str,
) -> OpSpec:
    """A write the engine does not serve yet, bound to the refusal handler."""
    from arbora.api.ops._common import build_write_op
    from arbora.api.ops.knowledge import EngineReply
    from arbora.api.registry import Verb

    return build_write_op(
        verb=Verb.ACT,
        op_id=op_id,
        summary=summary,
        examples=(example,),
        params=params,
        result=EngineReply,
        handler=f"{module}.handle_unserved",
        scope=scope,
    )
