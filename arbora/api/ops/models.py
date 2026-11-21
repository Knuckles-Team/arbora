"""Model list read operation (ARBORA-OPS-R024.5.1).

``models.list`` returns the deployment's configured chat or embedding models
as identity rows, so a model picker works for an ordinary user without the
admin ``ops:read`` scope that ``ops.config`` needs. Only the identity fields
are projected: endpoints, key references and headers never leave the
deployment configuration.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

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


class ModelsGetParams(Params):
    """The id is a body field because real model ids contain ``/``."""

    kind: str = Field(min_length=1, max_length=32)
    model_id: str = Field(min_length=1, max_length=256)


class ModelsGetResult(Params):
    value: dict[str, Any]


#: A locator, never a value (ARBORA-OPS-R922.2).
_SECRET_REF = re.compile(r"^(env|openbao)://\S+$")
#: Field names that hold credential material when not a locator.
_SECRET_NAME = re.compile(r"(?i)(key|token|secret|password|passwd|credential|auth)")
#: Fields already shown as identity or label.
_IDENTITY = frozenset({"id", "name", "provider"})


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


def _split_fields(
    entry: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Editable fields and secret references; a stored credential value is dropped."""
    fields: dict[str, Any] = {}
    refs: list[dict[str, str]] = []
    for name, value in entry.items():
        if name in _IDENTITY:
            continue
        if isinstance(value, str) and _SECRET_REF.match(value):
            refs.append({"name": name, "ref": value})
        elif _SECRET_NAME.search(name):
            continue
        elif isinstance(value, Mapping):
            nested = {
                key: item
                for key, item in value.items()
                if not (_SECRET_NAME.search(str(key)) or _is_ref(item))
            }
            refs.extend(
                {"name": f"{name}.{key}", "ref": item}
                for key, item in value.items()
                if _is_ref(item)
            )
            fields[name] = nested
        else:
            fields[name] = value
    return fields, refs


def _is_ref(value: Any) -> bool:
    return isinstance(value, str) and _SECRET_REF.match(value) is not None


async def handle_models_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One model's editable fields and its secret references, never values."""
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.core.settings import runtime_config

    kind = params["kind"]
    if kind not in _SETTING:
        raise OperationRefused("UNKNOWN_KIND", {"kind": kind})
    raw = getattr(runtime_config, _SETTING[kind], None)
    for entry in _entries(raw):
        if str(entry.get("id", "")) != params["model_id"]:
            continue
        fields, refs = _split_fields(entry)
        model = {
            "id": entry["id"],
            "label": entry.get("name") or entry["id"],
            "provider": entry.get("provider"),
            "fields": fields,
        }
        return {"value": {"kind": kind, "model": model, "secret_refs": refs}}
    raise OperationRefused("NOT_FOUND")


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
        build_read_op(
            op_id="models.get",
            summary="Read one model's editable fields and secret references",
            examples=("show the settings of this model",),
            params=ModelsGetParams,
            result=ModelsGetResult,
            handler="arbora.api.ops.models.handle_models_get",
            scope="ops:admin",
        ),
    )


specs = operations

__all__ = [
    "MODEL_FIELDS",
    "ModelsGetParams",
    "ModelsListParams",
    "handle_models_get",
    "handle_models_list",
    "operations",
]
