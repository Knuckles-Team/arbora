"""One governed ``fleet.call`` from inside a served handler (ARBORA-FLEET-R024.12).

Handlers that must reach a connector (a rollback, a scale, a chat post) go through the
same admission path as the served ``fleet.call`` operation: the gateway decides the
effect, then dispatches under the verified caller and the service identity of the
request. No handler keeps a client of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused


async def call_fleet_tool(
    context: Any, server: str, tool: str, arguments: Mapping[str, Any]
) -> Any:
    """Dispatch one connector tool through the request's fleet gateway."""
    from arbora.api.ops import fleet

    gateway = context.services.get("fleet_gateway")
    if gateway is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "fleet gateway is not bound"})
    call_op = next(op for op in fleet.operations() if op.id == "fleet.call")
    params = {"server": server, "tool": tool, "arguments": dict(arguments)}
    caller = context.caller
    decision = await gateway.effect(call_op, params, caller)
    return await gateway.call(
        caller,
        server,
        tool,
        dict(arguments),
        expected_effect=decision.effect,
        service_identity=context.service_identity,
        owner=context.owner,
        owner_ref=getattr(context, "owner_ref", None),
        fleet_decision=decision,
        registry_digest=context.registry_digest,
    )


def refused_result(result: Any) -> bool:
    """Whether a connector answered with an error instead of a result."""
    if isinstance(result, Mapping):
        return bool(result.get("error") or result.get("isError"))
    return isinstance(result, str) and result.startswith("Error:")
