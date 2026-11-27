"""Delivery writes to the code host are governed effects (ARBORA-OPS-R896.11).

Every outside write of a delivery (push, open, comment, thread reply, thread resolve, close, arm,
disarm) is one governed effect ``{effect_kind, target, payload_digest, idempotency_key}`` whose
key is ``delivery_id:assignment_id:step``. The effect's reservation lives in the engine key-value
store: ``pending`` before the connector call, ``done`` with the receipt after it. A restart
leaves an effect with no receipt ``pending``, which is ``uncertain``: a retry refuses
``EFFECT_UNCERTAIN`` until a read of the connector (``reconcile``) records it ``done`` or
removes the reservation. Arbora keeps no other effect log.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_write_op
from arbora.api.ops.engine_calls import engine_send, unavailable

EFFECT_KINDS = (
    "push",
    "open",
    "comment",
    "thread_reply",
    "thread_resolve",
    "close",
    "arm",
    "disarm",
)
NAMESPACE = "development.delivery.effect"
WRITE_SCOPE = "lifecycle:write"
EffectKind = Literal[
    "push",
    "open",
    "comment",
    "thread_reply",
    "thread_resolve",
    "close",
    "arm",
    "disarm",
]
_ID = Field(min_length=1, max_length=128)


class DeliveryEffectParams(Params):
    delivery_id: str = _ID
    assignment_id: str = _ID
    step: str = _ID
    effect_kind: EffectKind
    target: str = Field(min_length=1, max_length=512)
    payload_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    server: str = _ID
    tool: str = _ID


class ReconcileParams(Params):
    delivery_id: str = _ID
    assignment_id: str = _ID
    step: str = _ID
    server: str = _ID
    tool: str = _ID


class EffectReceipt(Params):
    idempotency_key: str
    state: Literal["done", "absent"]
    receipt: dict[str, Any] | None = None


def delivery_effect_key(delivery_id: str, assignment_id: str, step: str) -> str:
    """The idempotency key of one delivery effect."""
    return f"{delivery_id}:{assignment_id}:{step}"


async def _reservation(context: Any, key: str) -> tuple[int, dict[str, Any] | None]:
    entry = await engine_send(context, "KvGet", {"namespace": NAMESPACE, "key": key})
    if not isinstance(entry, Mapping) or entry.get("value") is None:
        return 0, None
    raw = entry["value"]
    value = json.loads(bytes(raw)) if isinstance(raw, (bytes, bytearray)) else raw
    return int(entry.get("version", 1)), dict(value)


async def _store(
    context: Any, key: str, version: int, record: Mapping[str, Any]
) -> None:
    swapped = await engine_send(
        context,
        "KvCas",
        {
            "namespace": NAMESPACE,
            "key": key,
            "expected_version": version,
            "new": json.dumps(dict(record)).encode(),
        },
    )
    if isinstance(swapped, Mapping) and swapped.get("swapped") is False:
        raise OperationRefused("EFFECT_UNCERTAIN", {})


def _receipt(answer: Any) -> dict[str, Any]:
    value = answer.get("value", answer) if isinstance(answer, Mapping) else None
    receipt = value.get("receipt") if isinstance(value, Mapping) else None
    if not isinstance(receipt, Mapping) or not all(
        isinstance(receipt.get(f), str) and receipt[f]
        for f in ("effect_id", "provider_ref", "applied_at")
    ):
        raise OperationRefused("RECEIPT_MISMATCH", {})
    return {f: receipt[f] for f in ("effect_id", "provider_ref", "applied_at")}


async def handle_delivery_effect(
    context: Any, params: Mapping[str, Any], op: Any
) -> dict[str, Any]:
    """Apply one effect once: a done key returns its receipt, a pending key refuses."""
    from arbora.api.ops.research import FleetCaller
    from arbora.fleet.service_child import ServiceChildOutcomeUnknown

    key = delivery_effect_key(
        params["delivery_id"], params["assignment_id"], params["step"]
    )
    version, held = await _reservation(context, key)
    state = (held or {}).get("state")
    if state == "done":
        return {"idempotency_key": key, "state": "done", "receipt": held["receipt"]}  # type: ignore[index]
    if state == "pending":
        raise OperationRefused("EFFECT_UNCERTAIN", {})
    await _store(context, key, version, {"state": "pending"})
    effect = {
        "effect_kind": params["effect_kind"],
        "target": params["target"],
        "payload_digest": params["payload_digest"],
        "idempotency_key": key,
    }
    try:
        answer = await FleetCaller(context).call(
            params["server"], params["tool"], {"request": effect}
        )
    except ServiceChildOutcomeUnknown:
        raise OperationRefused("EFFECT_UNCERTAIN", {}) from None
    receipt = _receipt(answer)
    await _store(context, key, version + 1, {"state": "done", "receipt": receipt})
    return {"idempotency_key": key, "state": "done", "receipt": receipt}


async def handle_delivery_effect_reconcile(
    context: Any, params: Mapping[str, Any], op: Any
) -> dict[str, Any]:
    """Read the connector for the effect's key; record it done, or clear the reservation."""
    from arbora.api.ops.research import FleetCaller

    key = delivery_effect_key(
        params["delivery_id"], params["assignment_id"], params["step"]
    )
    version, held = await _reservation(context, key)
    if held is None or held.get("state") == "done":
        return {
            "idempotency_key": key,
            "state": "done" if held else "absent",
            "receipt": (held or {}).get("receipt"),
        }
    answer = await FleetCaller(context).call(
        params["server"], params["tool"], {"idempotency_key": key}
    )
    value = answer.get("value", answer) if isinstance(answer, Mapping) else {}
    if not isinstance(value, Mapping) or value.get("state") not in (
        "applied",
        "absent",
    ):
        raise unavailable("connector effect status malformed")
    if value["state"] == "absent":
        await _store(context, key, version, {"state": "absent"})
        return {"idempotency_key": key, "state": "absent", "receipt": None}
    receipt = _receipt(value)
    await _store(context, key, version, {"state": "done", "receipt": receipt})
    return {"idempotency_key": key, "state": "done", "receipt": receipt}


NATIVE_IDEMPOTENCY: dict[str, str] = {
    # The engine key-value reservation of the effect key (compare-and-set) is the replay owner.
    "development.delivery.effect": "medulla:KvCas",
    "development.delivery.effect.reconcile": "medulla:KvCas",
}


def operations() -> tuple[Any, ...]:
    module = "arbora.api.ops.development_effects"
    return (
        build_write_op(
            op_id="development.delivery.effect",
            summary="Apply one delivery write to the code host as a governed effect",
            examples=("comment on the delivery's pull request",),
            params=DeliveryEffectParams,
            result=EffectReceipt,
            handler=f"{module}.handle_delivery_effect",
            scope=WRITE_SCOPE,
        ),
        build_write_op(
            op_id="development.delivery.effect.reconcile",
            summary="Read the code host for an uncertain delivery effect and settle its reservation",
            examples=("check whether the uncertain effect was applied",),
            params=ReconcileParams,
            result=EffectReceipt,
            handler=f"{module}.handle_delivery_effect_reconcile",
            scope=WRITE_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "EFFECT_KINDS",
    "NATIVE_IDEMPOTENCY",
    "delivery_effect_key",
    "handle_delivery_effect",
    "handle_delivery_effect_reconcile",
    "operations",
    "specs",
]
