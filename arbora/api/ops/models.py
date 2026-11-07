"""Model list read operation (ARBORA-OPS-R024.5.1).

``models.list`` returns the deployment's configured chat or embedding models
as identity rows, so a model picker works for an ordinary user without the
admin ``ops:read`` scope that ``ops.config`` needs. Only the identity fields
are projected: endpoints, key references and headers never leave the
deployment configuration.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel

from arbora.api.ops._common import Params, build_read_op
from arbora.api.registry import OpSpec

#: The only fields a model row exposes.
MODEL_FIELDS = ("id", "name", "provider", "model", "model_id", "description")
#: An existing engine scope of the user class (the engine registry owns scope names).
MODELS_READ_SCOPE = "registry:read"

ModelKind = Literal["chat", "embedding"]
_SETTING = {"chat": "chat_models", "embedding": "embedding_models"}


class ModelsListParams(Params):
    kind: ModelKind = "chat"


class ModelsListResult(Params):
    value: dict[str, Any]


def _entries(raw: Any) -> list[Mapping[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    rows: list[Mapping[str, Any]] = []
    for item in raw:
        if isinstance(item, BaseModel):
            item = item.model_dump(mode="json")
        if isinstance(item, Mapping):
            rows.append(item)
    return rows


async def handle_models_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """List the configured models of one kind as identity rows."""
    from arbora.core.settings import runtime_config

    kind = params.get("kind", "chat")
    raw = getattr(runtime_config, _SETTING[kind], None)
    models = [
        {name: entry[name] for name in MODEL_FIELDS if name in entry}
        for entry in _entries(raw)
    ]
    return {"value": {"kind": kind, "models": models}}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="models.list",
            summary="List the configured chat or embedding models for a model picker",
            examples=("which chat models can I pick",),
            params=ModelsListParams,
            result=ModelsListResult,
            handler="arbora.api.ops.models.handle_models_list",
            scope=MODELS_READ_SCOPE,
        ),
    )


specs = operations

__all__ = ["MODEL_FIELDS", "ModelsListParams", "handle_models_list", "operations"]
