"""Serve a mapped legacy MCP action through the registry's one ``invoke`` pipeline.

``require_served_action`` decides what a retired action means; this module
makes a mapped action *run*. The verified caller comes from the MCP request's
session exactly as the MCP verbs derive it (``VerifiedMCPCaller`` through the
served projection), and the call goes to ``invoke`` with the mapped operation
id, so admission, policy, audit and refusal are the pipeline's own. No retired
handler is called (ARBORA-HOST-R005.2.9).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from arbora.api.errors import ArboraErrorCode, ArboraRefusal
from arbora.mcp_server.legacy_action_mapping import (
    ResolvedAction,
    require_served_action,
)

#: Legacy argument names that the mapped operation names differently.
_ARGUMENT_FIELD: Mapping[tuple[str, str], str] = {("query.nl", "query"): "text"}


def _declared_fields(spec: Any) -> frozenset[str] | None:
    """The mapped operation's parameter names, or ``None`` for an engine schema."""
    return (
        frozenset(model_fields)
        if (model_fields := getattr(spec.params, "model_fields", None))
        else None
    )


def operation_params(
    resolved: ResolvedAction, arguments: Mapping[str, Any], spec: Any
) -> dict[str, Any]:
    """The mapped call's parameters: the entry's fixed ones plus the supplied arguments.

    An argument is forwarded when it is set and the operation declares it (an
    engine-schema operation validates its own names). The entry's fixed
    parameters always win.
    """
    declared = _declared_fields(spec)
    params: dict[str, Any] = {}
    for name, value in arguments.items():
        if value in ("", None, (), [], {}):
            continue
        field = _ARGUMENT_FIELD.get((resolved.operation or "", name), name)
        if declared is None or field in declared:
            params[field] = value
    params.update(resolved.params)
    return params


def _idempotency_key(spec: Any, caller: Any) -> str | None:
    """The request's own reference for a write that is not naturally idempotent.

    The retired tools carried no key; one MCP request is one effect, so its
    reference names it, and a replay of that request is the pipeline's replay.
    """
    from arbora.api.registry import Effect, Idempotency

    if spec.effect != Effect.WRITE or spec.idempotency == Idempotency.NATURAL:
        return None
    return str(getattr(caller, "request_id", "") or "") or None


async def invoke_legacy_action(
    action: str, arguments: Mapping[str, Any], *, tool: str | None = None
) -> str:
    """Resolve ``action`` and run it through ``invoke`` as the verified caller.

    A retired action answers ``UNAVAILABLE`` and an unknown one
    ``INVALID_ARGUMENT`` (both raised by ``require_served_action``). A request
    with no verified caller answers ``UNAUTHENTICATED`` before any operation.
    The pipeline's envelope, success or refusal, is the text returned.
    """
    from arbora.api.invoke.steps import surface_caller_admitted
    from arbora.api.mcp.verbs import _invoke_and_render
    from arbora.mcp_server import runtime

    resolved = require_served_action(action, tool)
    projection = runtime.SERVED_PROJECTION
    if projection is None:
        raise ArboraRefusal(
            code=ArboraErrorCode.UNAVAILABLE,
            message="The served operation projection is not composed",
            details={"action": action},
        )
    caller = projection.caller_for_request()
    if not surface_caller_admitted(caller):
        raise ArboraRefusal(
            code=ArboraErrorCode.UNAUTHENTICATED,
            message="A verified caller is required",
            details={"action": action},
        )
    spec = projection.registry.get(resolved.operation)
    if spec is None:
        raise ArboraRefusal(
            code=ArboraErrorCode.UNKNOWN_OP,
            message=f"Mapped operation {resolved.operation!r} is not registered",
            details={"action": action, "operation": resolved.operation},
        )
    envelope = await _invoke_and_render(
        projection,
        caller,
        resolved.operation,
        operation_params(resolved, arguments, spec),
        plan_ref=None,
        idempotency_key=_idempotency_key(spec, caller),
        resolution=None,
    )
    return json.dumps(envelope, default=str, sort_keys=True)
