"""Attended browser-control operations over the existing WebUI authority."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.domain.browser_control.browser_control_api import (
    BrowserDiscoveryReceipt,
)
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import bound_service, build_write_op
from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    OpSpec,
    PrincipalRule,
    Surface,
    Verb,
)


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
    from arbora.security.session import use_session

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


class DesktopActionParams(BaseModel):
    """One native desktop action under a tenant-bound sandbox grant."""

    model_config = ConfigDict(extra="forbid")
    grant_id: str = Field(min_length=1, max_length=256)
    kind: Literal["click", "type", "verify"]
    target: str = Field(min_length=1, max_length=512)
    text: str = Field(default="", max_length=4096)


class DesktopActionResult(BaseModel):
    value: dict[str, Any]


async def _admit_desktop_grant(context: Any, grant_id: str, target: str) -> Any:
    """The grant, or the refusal that stops the call before any provider is reached."""
    grants = bound_service(
        context, "desktop_grants", reason="the desktop grant store is not composed"
    )
    grant = await grants.get(grant_id)
    if grant is None:
        raise OperationRefused("DESKTOP_GRANT_MISSING", {"grant_id": grant_id})
    if getattr(grant, "tenant", None) != context.caller.tenant:
        # A foreign grant answers exactly like an absent one except its code.
        raise OperationRefused("DESKTOP_GRANT_CROSS_TENANT", {"grant_id": grant_id})
    if getattr(grant, "revoked", False):
        raise OperationRefused("DESKTOP_GRANT_REVOKED", {"grant_id": grant_id})
    if target not in tuple(getattr(grant, "targets", ())):
        raise OperationRefused("DESKTOP_GRANT_MISSING", {"grant_id": grant_id})
    return grant


async def handle_desktop_action(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Grant check first, then the SDK desktop port; never a local driver."""
    from arbora.api.ops.pin_guards import refusal
    from arbora.pin_lag.scion_sdk import require_desktop
    from arbora.pin_lag.unavailable import PinLagUnavailable

    if context.caller.session is None:
        raise OperationRefused("UNAUTHENTICATED", {"reason": "verified session"})
    grant = await _admit_desktop_grant(context, params["grant_id"], params["target"])
    try:
        sdk = require_desktop()
    except PinLagUnavailable as error:
        raise refusal(error) from error
    provider = bound_service(
        context, "desktop_provider", reason="the desktop provider is not composed"
    )
    action = sdk.DesktopAction(
        kind=sdk.DesktopActionKind(params["kind"]),
        target=params["target"],
        text=params["text"],
    )
    scope = sdk.DesktopGrant(targets=tuple(grant.targets))
    result = await asyncio.to_thread(provider.perform, action, scope)
    return {
        "value": {
            "ok": bool(result.ok),
            "evidence": str(result.evidence),
            "grant_id": params["grant_id"],
        }
    }


class VisualStateParams(BaseModel):
    """The observation a run reads or acts on: run, source, frame and revision."""

    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=512)
    frame: str = Field(min_length=1, max_length=512)
    revision: int = Field(ge=0)


class VisualStateResult(BaseModel):
    value: dict[str, Any]


async def handle_visual_state(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Pass run, tenant and the observation to the engine; hold no state here.

    The engine's visual-state records (``MEDULLA-DECISION-ENGINE-R816``) answer
    reusable state or a refusal (``VISUAL_STATE_STALE``,
    ``VISUAL_STATE_CROSS_TENANT``), which pass through unchanged.
    """
    states = context.services.get("visual_state")
    if states is None:
        from arbora.api.errors import not_served

        raise not_served("browser.visual_state", "MEDULLA-DECISION-ENGINE-R816")
    answer = await states.resolve(
        action=op.id.removeprefix("browser."),
        tenant=context.caller.tenant,
        run_id=params["run_id"],
        source=params["source"],
        frame=params["frame"],
        revision=params["revision"],
    )
    refused = getattr(answer, "refusal", None)
    if refused:
        raise OperationRefused(str(refused), {"run_id": params["run_id"]})
    return {
        "value": {
            "state": getattr(answer, "state", None),
            "provenance": {
                "source": params["source"],
                "frame": params["frame"],
                "revision": params["revision"],
            },
        }
    }


def _visual_ops() -> tuple[OpSpec, ...]:
    return tuple(
        build_write_op(
            op_id=f"browser.{name}",
            summary=summary,
            examples=(example,),
            params=VisualStateParams,
            result=VisualStateResult,
            handler="arbora.api.ops.browser.handle_visual_state",
            scope="kg:write",
            surfaces=frozenset({Surface.MCP, Surface.HTTP}),
        )
        for name, summary, example in (
            (
                "observe",
                "Record a run-bound observation and read its reusable visual state",
                "observe my browser frame for this run",
            ),
            (
                "act",
                "Act on the run-bound visual state of the last observation",
                "act on the last observed frame",
            ),
        )
    )


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
        build_write_op(
            op_id="browser.desktop_action",
            summary="Click, type or verify on a granted native desktop target",
            examples=("click the granted desktop button",),
            params=DesktopActionParams,
            result=DesktopActionResult,
            handler="arbora.api.ops.browser.handle_desktop_action",
            scope="kg:write",
            surfaces=frozenset({Surface.MCP, Surface.HTTP}),
        ),
        *_visual_ops(),
        OpSpec(
            id="browser.request_call",
            verb=Verb.ACT,
            summary="Propose an exact browser action for attended human acceptance",
            examples=("ask the attended browser owner to navigate",),
            params=BrowserParams,
            result=BrowserResult,
            binding=handler,
            scopes=frozenset({"kg:write"}),
            effect=Effect.WRITE,
            surfaces=frozenset({Surface.MCP}),
            audit=AuditClass.EVENT,
        ),
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
