"""Hub federation served as an operation (ARBORA-A2A-R024).

``a2a.federation.forward`` reads one topic group with the engine ``StreamRead``,
forwards it to one registered peer (``a2a.peer.*``, ARBORA-A2A-R001.7) through the
composed ``a2a_peer_transport`` and appends each reply with ``StreamPublish``.

Every engine call is made as the verified caller, so the engine enforces who may read
and publish the topic stream. ``topic_scope`` names the prefixes this forward may
touch; it can only narrow, because the forward scope is its intersection with the
peer's registered scope and a peer registered wider is refused
``FEDERATION_SCOPE_WIDER``. The write replays under the engine ``StreamPublish`` key,
one key per reply, derived from the request's idempotency key.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import Field

from arbora.api.domain.a2a.hub_federation import FederationRefused, HubFederation
from arbora.api.invoke.bootstrap import caller_dispatch_graph
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.ops.a2a_peers import _registry
from arbora.api.ops.engine_calls import send_on_client
from arbora.api.registry import OpSpec
from arbora.core.claims import claim_values
from arbora.security.anonymous import client_scope

WRITE_SCOPE = "bus:write"
#: Writes replay under the engine method that holds the record.
NATIVE_IDEMPOTENCY: Mapping[str, str] = MappingProxyType(
    {
        "a2a.federation.forward": "medulla:StreamPublish",
    }
)


class FederationForwardParams(_common.Params):
    peer: str = Field(min_length=1, max_length=128)
    topic: str = Field(min_length=1, max_length=256)
    #: The topic prefixes this forward may touch; the peer's scope must sit inside it.
    topic_scope: list[str] = Field(min_length=1, max_length=64)
    from_offset: int = Field(default=0, ge=0)
    limit: int = Field(default=200, ge=1, le=200)


class FederationForwardResult(_common.Params):
    value: dict[str, Any]


def _now_s(context: Any) -> float:
    clock = context.services.get("clock")
    return int(clock()) / 1000 if clock is not None else time.time()


def _engine_send(context: Any) -> Any:
    """``send(method, params, key)`` as the verified caller; a write needs a key."""

    async def send(method: str, params: dict[str, Any], key: str | None) -> Any:
        graph = await caller_dispatch_graph(context)
        with client_scope(context.client, claim_values(context.caller.engine_claims)):
            return await send_on_client(context.client, method, params, graph, key=key)

    return send


async def handle_federation_forward(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = FederationForwardParams.model_validate(dict(params))
    transport = _common.bound_service(
        context, "a2a_peer_transport", reason="the A2A peer transport is not composed"
    )
    registry = _registry(context)
    federation = HubFederation(
        _engine_send(context),
        registry.lookup,
        transport,
        clock=lambda: _now_s(context),
    )
    try:
        value = await federation.forward(
            tenant=str(context.caller.tenant),
            peer_name=request.peer,
            topic=request.topic,
            caller_scope=request.topic_scope,
            from_offset=request.from_offset,
            limit=request.limit,
            idempotency_key=context.idempotency_key,
        )
    except FederationRefused as refusal:
        raise OperationRefused(refusal.code) from None
    return {"value": value}


def operations() -> tuple[OpSpec, ...]:
    return (
        _common.build_write_op(
            op_id="a2a.federation.forward",
            summary="Forward a topic message group to a registered A2A peer hub",
            examples=("forward this topic to a peer hub",),
            params=FederationForwardParams,
            result=FederationForwardResult,
            handler="arbora.api.ops.a2a_federation.handle_federation_forward",
            scope=WRITE_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "handle_federation_forward",
    "operations",
    "specs",
]
