"""The layered configuration read (ARBORA-HARNESS-PLUGINS-R013).

``config.layers.get`` forwards the engine ``ConfigLayers`` read: the company,
team and user layers the verified caller may see, in application order. An
absent, foreign or hidden layer is left out by the engine. The repository,
directory and local layers stay the client's own files.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops._engine import engine_send
from arbora.api.ops.knowledge import EngineReply
from arbora.api.registry import OpSpec

SCOPE = "policy:read"


class ConfigLayersParams(Params):
    pass


async def handle_config_layers(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return {"value": await engine_send(context, "ConfigLayers", {})}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="config.layers.get",
            summary="Read the company, team and user configuration layers",
            examples=("what configuration layers apply to me",),
            params=ConfigLayersParams,
            result=EngineReply,
            handler="arbora.api.ops.config_layers.handle_config_layers",
            scope=SCOPE,
        ),
    )


specs = operations

__all__ = ["handle_config_layers", "operations", "specs"]
