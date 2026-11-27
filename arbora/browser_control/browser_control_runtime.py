"""Process binding and governed caller adapter for the one browser-control service."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel

from arbora.browser_control.browser_control_api import (
    BrowserCallRequest,
    BrowserDiscoveryReceipt,
    BrowserDiscoveryRequest,
    BrowserProposalRequest,
    CancelCallRequest,
    IssueLeaseRequest,
    ReconcileCallRequest,
    RevokeLeaseRequest,
)
from arbora.browser_control.browser_state import BrowserState
from arbora.security.session import resolve_session

BrowserControlAction = Literal[
    "discover",
    "request_call",
    "issue_lease",
    "execute_call",
    "cancel_call",
    "reconcile_call",
    "revoke_lease",
]


async def _invoke(browser: BrowserState, method: str, request: BaseModel) -> BaseModel:
    service, owner_loop = browser.authority()
    operation = getattr(service, method, None)
    if not callable(operation):
        raise RuntimeError("browser-control service contract is incomplete")
    if asyncio.get_running_loop() is owner_loop:
        return await operation(request)
    future = asyncio.run_coroutine_threadsafe(operation(request), owner_loop)
    try:
        return await asyncio.wrap_future(future)
    except asyncio.CancelledError:
        future.cancel()
        raise


async def dispatch_browser_control(
    browser: BrowserState, action: BrowserControlAction, payload: dict[str, Any]
) -> BaseModel:
    """Authorize and invoke one caller action on the WebUI-owned service."""

    resolve_session(required_scope="kg:read" if action == "discover" else "kg:write")
    if action == "discover":
        discovery_request = BrowserDiscoveryRequest.model_validate(payload)
        available = browser.attached()
        if not available:
            return BrowserDiscoveryReceipt(bridge_available=False)
        return await _invoke(browser, "discover", discovery_request)
    contracts: dict[str, tuple[type[BaseModel], str]] = {
        "request_call": (BrowserProposalRequest, "request_call"),
        "issue_lease": (IssueLeaseRequest, "issue_lease"),
        "execute_call": (BrowserCallRequest, "execute_call"),
        "cancel_call": (CancelCallRequest, "cancel_call"),
        "reconcile_call": (ReconcileCallRequest, "reconcile_call"),
        "revoke_lease": (RevokeLeaseRequest, "revoke_lease"),
    }
    contract = contracts.get(action)
    if contract is None:
        raise ValueError("unsupported browser-control action")
    model, method = contract
    request = model.model_validate(payload)
    return await _invoke(browser, method, request)


__all__ = [
    "BrowserControlAction",
    "dispatch_browser_control",
]


async def dispatch_browser_operation(
    browser: BrowserState,
    action: BrowserControlAction,
    payload: dict[str, Any],
    caller: Any,
) -> BaseModel:
    """Adapt the governed operation contract without minting caller authority."""
    resolve_session(caller.session)
    return await dispatch_browser_control(browser, action, payload)
