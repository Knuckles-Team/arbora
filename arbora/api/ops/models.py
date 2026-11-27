"""Model read operations (ARBORA-OPS-R878, R922.2).

``models.list`` lists the ``ModelProfile`` components the engine holds and the verified
caller may see (ARBORA-OPS-R878). The graph is the authority: there is no configuration
read, so a profile added to the graph shows at once and a foreign tenant's profile never
does. Only identity fields are projected. ``models.get`` reads one configured model's
editable fields for the admin editor (ARBORA-OPS-R922.2).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops.engine_calls import decode, engine_send
from arbora.api.registry import OpSpec

#: The engine label of the model profile components.
MODEL_PROFILE_LABEL = "ModelProfile"
MODELS_READ_SCOPE = "kg:read"
#: The most rows one page returns.
MAX_MODELS = 200

_SETTING = {"chat": "chat_models", "embedding": "embedding_models"}


class ModelsListParams(Params):
    limit: int = Field(default=MAX_MODELS, ge=1, le=MAX_MODELS)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


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


def _profile(row: Any) -> dict[str, Any] | None:
    """One profile as ``id``, ``display_name`` and ``provider``; any other row is skipped."""
    if not (isinstance(row, (list, tuple)) and len(row) == 2):
        return None
    node_id, props = row
    if isinstance(props, BaseModel):
        props = props.model_dump(mode="json")
    if not isinstance(props, Mapping):
        return None
    return {
        "id": str(node_id),
        "display_name": str(props.get("display_name") or props.get("name") or ""),
        "provider": str(props.get("provider") or ""),
    }


async def handle_models_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """List the profiles the engine shows this caller; arbora filters nothing itself."""
    limit = int(params.get("limit", MAX_MODELS))
    request: dict[str, Any] = {"label": MODEL_PROFILE_LABEL, "limit": limit + 1}
    if params.get("cursor"):
        request["after"] = params["cursor"]
    rows = list(decode(await engine_send(context, "GetNodesByLabel", request)) or ())
    profiles = [p for p in map(_profile, rows[:limit]) if p is not None]
    more = len(rows) > limit
    return {
        "value": {
            "models": profiles,
            "next_cursor": profiles[-1]["id"] if more and profiles else None,
        }
    }


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

    kind = params["kind"]
    if kind not in _SETTING:
        raise OperationRefused("UNKNOWN_KIND", {"kind": kind})
    raw = getattr(context.services["config"], _SETTING[kind], None)
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
            summary="List the model profiles the caller may prefer",
            examples=("which model profiles can I pick",),
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
    "MAX_MODELS",
    "MODEL_PROFILE_LABEL",
    "ModelsGetParams",
    "ModelsListParams",
    "handle_models_get",
    "handle_models_list",
    "operations",
]
