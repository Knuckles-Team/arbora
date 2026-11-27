"""The mode transition operation (ARBORA-IDENTITY-R014.1).

``identity.mode_transition`` forwards one engine ``Identity`` ``config.transition`` request
(``MEDULLA-IDENTITY-R003.7``). Arbora decides no precondition: the engine owns the epoch
check, the legal edges and the refusal codes, and they pass through with the engine code.
The operation previews first and needs the administrator approval of the plan flow.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.registry import (
    AuditClass,
    Composite,
    Confirm,
    Effect,
    Idempotency,
    OpSpec,
    PrincipalRule,
    Surface,
    Verb,
)
from arbora.identity.engine_admin import IdentityRefused

ADMIN_SCOPE = "identity:admin"
#: The engine owns replay of the transition by its expected epoch.
NATIVE_IDEMPOTENCY: Mapping[str, str] = MappingProxyType(
    {"identity.mode_transition": "medulla:Identity"}
)


class ModeTransitionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: Literal["local", "external"]
    expected_epoch: int = Field(ge=0)


class ModeTransitionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: dict[str, Any]


async def handle_mode_transition(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    port = context.services.get("identity_admin")
    if port is None:
        raise RuntimeError("identity admin port is not bound")
    caller = context.caller
    try:
        current = await port.identity(
            context.client, caller.engine_claims, "config", "get", None, expect="config"
        )
        config = await port.identity(
            context.client,
            caller.engine_claims,
            "config",
            "transition",
            {
                "to": params["to"],
                "expected_epoch": params["expected_epoch"],
                "issuer_kid": current.get("issuer_kid_current"),
            },
            expect="config",
        )
    except IdentityRefused as refused:
        # The engine's own code passes through; nothing here judges the precondition.
        raise OperationRefused(refused.code) from None
    return {"value": {"mode": config.get("mode"), "epoch": config.get("epoch")}}


def operations() -> tuple[OpSpec, ...]:
    return (
        OpSpec(
            id="identity.mode_transition",
            verb=Verb.MANAGE,
            summary="Change the authentication mode at an expected epoch",
            examples=("switch the identity mode",),
            params=ModeTransitionParams,
            result=ModeTransitionResult,
            binding=Composite(
                handler="arbora.api.ops.identity_mode.handle_mode_transition"
            ),
            scopes=frozenset({ADMIN_SCOPE}),
            effect=Effect.ADMIN,
            principals=PrincipalRule.HUMAN_UNDELEGATED,
            confirm=Confirm.PLAN,
            surfaces=frozenset(
                {Surface.MCP, Surface.HTTP, Surface.A2A, Surface.CONSOLE}
            ),
            idempotency=Idempotency.KEY_REQUIRED,
            audit=AuditClass.EVENT,
        ),
    )


specs = operations
