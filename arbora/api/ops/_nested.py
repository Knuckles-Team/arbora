"""One registered operation invoked from inside another served handler.

An operation that is a name for another (a command, a function, a thin alias)
runs the target through the same invoke pipeline as the caller's own request.
The target's scopes, effect gates and audit apply to the caller unchanged, so a
name never grants what the target would refuse (ARBORA-OPS-R908, R913).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused, invoke
from arbora.api.invoke.steps import OpError
from arbora.api.ops._engine import unavailable
from arbora.api.registry import OpSpec, Surface


def registered(context: Any, op_id: str) -> OpSpec | None:
    """The served operation of that id, or ``None`` when absent or not composed."""
    services = getattr(context, "operation_services", None)
    return None if services is None else services.registry.get(op_id)


async def invoke_registered(
    context: Any,
    op_id: str,
    arguments: Mapping[str, Any],
    *,
    only: Callable[[OpSpec], bool] | None = None,
) -> Any:
    """Run one registered operation as the caller; refuse what is not allowed.

    ``only`` limits the targets (a function mark, a read effect). A target that
    is absent or filtered out answers ``NOT_FOUND``, the same either way.
    """
    services = getattr(context, "operation_services", None)
    if services is None:
        raise unavailable("the operation registry is not composed")
    target = services.registry.get(op_id)
    if target is None or (only is not None and not only(target)):
        raise OperationRefused("NOT_FOUND")
    outer = getattr(context, "idempotency_key", None)
    result = await invoke(
        op_id,
        dict(arguments),
        context.caller,
        Surface.HTTP,
        services=services,
        idempotency_key=f"{outer}:{op_id}" if outer else None,
    )
    if isinstance(result, OpError):
        raise OperationRefused(result.code, dict(result.details))
    return result.value
