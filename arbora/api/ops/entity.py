"""Cross-application entity link approval (ARBORA-OPS-R887).

``entity.approve_link`` approves one proposed ``owl:sameAs`` link between two attached
application records. The engine's ``ApproveEntityLink`` (engine ``main``, scope
``query:write``) takes the two record references and records the approving principal from
the verified caller: this operation never approves in its own name and never names an
approver in the request. The receipt, or a typed refusal (``LINK_NOT_APPROVABLE``,
``LINK_APPROVER_MISSING``, ``LINK_PROPOSAL_UNKNOWN``), passes through unchanged. The
method is not in the pinned release, so the operation refuses ``UNAVAILABLE`` naming it
until the re-pin.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.ops._common import Params, build_write_op
from arbora.api.ops.pin_guards import send_main
from arbora.api.registry import AuditClass, Confirm, Effect, OpSpec, Verb

SCOPE = "query:write"
_APPLICATION = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class EntityRef(Params):
    application: str = _APPLICATION
    entity_id: str = Field(min_length=1, max_length=512)


class EntityApproveLinkParams(Params):
    left: EntityRef
    right: EntityRef


class EntityApproveLinkResult(Params):
    value: dict[str, Any]


async def handle_entity_approve_link(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Forward the pair; the engine answers the receipt or its own refusal."""
    answer = await send_main(
        context,
        "ApproveEntityLink",
        {"left": dict(params["left"]), "right": dict(params["right"])},
        idempotent=True,
    )
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


#: Replays under the engine method that holds the approval, never here.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "entity.approve_link": "medulla:ApproveEntityLink"
}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_write_op(
            verb=Verb.ACT,
            effect=Effect.DESTRUCTIVE,
            op_id="entity.approve_link",
            summary="Approve a proposed link between two application records",
            examples=("approve this entity link",),
            params=EntityApproveLinkParams,
            result=EntityApproveLinkResult,
            handler=f"{__name__}.handle_entity_approve_link",
            scope=SCOPE,
            confirm=Confirm.PLAN,
        ).model_copy(update={"audit": AuditClass.IDENTITY_CHAIN}),
    )


specs = operations
