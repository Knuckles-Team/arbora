"""Served preferences over the engine KV modality (ARBORA-OPS-R024.7).

One JSON object per principal lives under the engine KV namespace
``preferences``, key ``user``. Arbora keeps no copy and reads no key of it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.memory import _decode, _encode, _payload, _send
from arbora.api.registry import OpSpec

#: EG replays each KV write by its idempotency key, so the engine owns replay.
NATIVE_IDEMPOTENCY: dict[str, str] = {"preferences.set": "medulla:KvPut/KvCas"}
#: The largest saved record, in bytes.
MAX_BYTES = 65_536
NAMESPACE = "preferences"
KEY = "user"
_TARGET = {"namespace": NAMESPACE, "key": KEY, "shared": False}


class PreferencesGetParams(Params):
    pass


class PreferencesSetParams(Params):
    value: dict[str, Any] = Field(default_factory=dict)
    #: Compare-and-set: write only at this version (``0`` = only when absent).
    expected_version: int | None = Field(default=None, ge=0)


class PreferencesResult(Params):
    value: dict[str, Any]


async def handle_preferences_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    entry = _payload(await _send(context, "KvGet", dict(_TARGET), None))
    found = isinstance(entry, Mapping) and "value" in entry
    return {
        "value": {
            "found": found,
            "value": _decode(entry["value"]) if found else {},
            "version": int(entry.get("version", 0)) if found else 0,
        }
    }


async def handle_preferences_set(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    from arbora.api.invoke.pipeline import OperationRefused

    encoded = _encode(params.get("value") or {})
    if len(encoded) > MAX_BYTES:
        raise OperationRefused("INVALID_ARGUMENT", {})
    expected = params.get("expected_version")
    if expected is None:
        receipt = _payload(
            await _send(
                context,
                "KvPut",
                {**_TARGET, "value": encoded},
                context.idempotency_key,
            )
        )
        swapped = True
    else:
        receipt = _payload(
            await _send(
                context,
                "KvCas",
                {**_TARGET, "expected_version": int(expected), "new": encoded},
                context.idempotency_key,
            )
        )
        swapped = bool(isinstance(receipt, Mapping) and receipt.get("swapped"))
    version = int(receipt.get("version", 0)) if isinstance(receipt, Mapping) else 0
    return {"value": {"swapped": swapped, "version": version}}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="preferences.get",
            summary="Read the caller's saved preferences with their version",
            examples=("show my preferences", "what settings did I save"),
            params=PreferencesGetParams,
            result=PreferencesResult,
            handler="arbora.api.ops.preferences.handle_preferences_get",
            scope="kv:read",
        ),
        build_write_op(
            op_id="preferences.set",
            summary="Save the caller's preferences, optionally compare-and-set",
            examples=("save my preferences", "update my settings"),
            params=PreferencesSetParams,
            result=PreferencesResult,
            handler="arbora.api.ops.preferences.handle_preferences_set",
            scope="kv:write",
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "handle_preferences_get",
    "handle_preferences_set",
    "operations",
    "specs",
]
