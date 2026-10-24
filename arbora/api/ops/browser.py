"""Attended browser-control operations over the existing WebUI authority."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    OpSpec,
    PrincipalRule,
    Surface,
    Verb,
)
from arbora.browser_control.browser_control_api import BrowserDiscoveryReceipt


class BrowserParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: dict[str, Any]


class BrowserResult(BaseModel):
    value: dict[str, Any]


class BrowserDiscoveryResult(BaseModel):
    value: BrowserDiscoveryReceipt


async def handle_browser(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Delegate to the WebUI-owned browser authority with its session checks."""
    from arbora.api.contracts import use_session

    if context.caller.session is None:
        raise RuntimeError("browser operation requires a verified session")
    dispatch_browser_control = context.services.get("browser_control")
    if dispatch_browser_control is None:
        raise RuntimeError("browser-control authority is not bound")
    action = op.id.removeprefix("browser.")
    with use_session(context.caller.session):
        receipt = await dispatch_browser_control(
            action, dict(params["payload"]), context.caller
        )
    return {"value": receipt.model_dump(mode="json")}


def operations() -> tuple[OpSpec, ...]:
    handler = Composite(handler="arbora.api.ops.browser.handle_browser")
    discovery = OpSpec(
        id="browser.discover",
        verb=Verb.FIND,
        summary="Discover my attended WebUI registrations and bridge availability",
        examples=("find my WebMCP browser tools",),
        params=BrowserParams,
        result=BrowserDiscoveryResult,
        binding=handler,
        scopes=frozenset({"kg:read"}),
        effect=Effect.READ,
        surfaces=frozenset({Surface.MCP, Surface.HTTP, Surface.CONSOLE}),
    )
    return (
        discovery,
        *tuple(
            OpSpec(
                id=f"browser.{action}",
                verb=Verb.ACT,
                summary=f"{action.replace('_', ' ').capitalize()} an attended browser call",
                examples=(f"{action.replace('_', ' ')} for my attended browser",),
                params=BrowserParams,
                result=BrowserResult,
                binding=handler,
                scopes=frozenset({"kg:write"}),
                effect=Effect.WRITE,
                principals=PrincipalRule.HUMAN_UNDELEGATED,
                surfaces=frozenset({Surface.HTTP, Surface.CONSOLE}),
                audit=AuditClass.EVENT,
            )
            for action in (
                "issue_lease",
                "execute_call",
                "cancel_call",
                "reconcile_call",
                "revoke_lease",
            )
        ),
    )


specs = operations
