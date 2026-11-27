"""Viewport bins and selection operations (ARBORA-OPS-R064).

The engine owns the binning formulas, the extrema and the selection tokens
(``ViewportQuery`` and ``ResolveViewportSelection``). Arbora checks its limits
before dispatch, transports the engine answer and applies no pagination or
decimation of its own. A request may lower a ceiling, never raise it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from medulla.generated.models import SelectionToken, ViewportRequest
from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops.engine_calls import engine_send
from arbora.api.registry import OpSpec
from arbora.core.settings import runtime_config

READ_SCOPE = "graph:read"
#: The engine answers this refusal for a stale source revision or selection token.
STALE_CODES = frozenset({"STALE_REVISION", "STALE_SOURCE_REVISION"})


class ViewportBinsParams(Params):
    request: ViewportRequest
    max_bins: int | None = Field(default=None, ge=1)


class SelectionResolveParams(Params):
    request: ViewportRequest
    token: SelectionToken


class ViewportReply(Params):
    value: dict[str, Any]


def _request(params: Mapping[str, Any]) -> dict[str, Any]:
    request = params["request"]
    return (
        request.model_dump(mode="json")
        if hasattr(request, "model_dump")
        else dict(request)
    )


def _byte_limit(request: Mapping[str, Any]) -> int:
    """The request's own ceiling: positive and never above the configured one."""
    ceiling = int(runtime_config.viewport_max_bytes)
    asked = int(request.get("byte_ceiling") or 0)
    if asked < 1 or asked > ceiling:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "byte_ceiling"})
    return asked


def _bin_limit(params: Mapping[str, Any]) -> int:
    ceiling = int(runtime_config.viewport_max_bins)
    asked = params.get("max_bins")
    if asked is None:
        return ceiling
    if int(asked) > ceiling:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "max_bins"})
    return int(asked)


def _within(answer: Any, byte_limit: int, bin_limit: int) -> dict[str, Any]:
    body = dict(answer) if isinstance(answer, Mapping) else {"result": answer}
    size = len(json.dumps(body, sort_keys=True, default=str).encode())
    bins = body.get("bins")
    if size > byte_limit or (isinstance(bins, list) and len(bins) > bin_limit):
        raise OperationRefused(
            "LOAD_CAP_EXCEEDED", {"max_bytes": byte_limit, "max_bins": bin_limit}
        )
    return body


async def handle_viewport_bins(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The engine's bins for one viewport, inside both ceilings."""
    request = _request(params)
    byte_limit, bin_limit = _byte_limit(request), _bin_limit(params)
    answer = await engine_send(context, "ViewportQuery", {"request": request})
    return {"value": _within(answer, byte_limit, bin_limit)}


async def handle_selection_resolve(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The subjects a selection token resolves to under the same viewport."""
    request = _request(params)
    byte_limit = _byte_limit(request)
    token = params["token"]
    token = token.model_dump(mode="json") if hasattr(token, "model_dump") else token
    answer = await engine_send(
        context,
        "ResolveViewportSelection",
        {"request": request, "token": token},
    )
    return {"value": _within(answer, byte_limit, int(runtime_config.viewport_max_bins))}


def operations() -> tuple[OpSpec, ...]:
    base = "arbora.api.ops.viewport."
    return (
        build_read_op(
            op_id="graph.viewport.bins",
            summary="Bin one viewport of a large source for a scatter or line view",
            examples=("bin this viewport",),
            params=ViewportBinsParams,
            result=ViewportReply,
            handler=base + "handle_viewport_bins",
            scope=READ_SCOPE,
        ),
        build_read_op(
            op_id="graph.selection.resolve",
            summary="Resolve a viewport selection token to its subjects",
            examples=("resolve this selection",),
            params=SelectionResolveParams,
            result=ViewportReply,
            handler=base + "handle_selection_resolve",
            scope=READ_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "handle_selection_resolve",
    "handle_viewport_bins",
    "operations",
    "specs",
]
