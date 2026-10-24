"""Volatile browser channel handles; durable authority remains in Arbora."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from arbora.browser_control.browser_control_api import (
    BrowserCallReceipt,
    BrowserCallRequest,
    BrowserChannelBinding,
    CatalogRegistrationReceipt,
)
from arbora.browser_control.browser_control_binding import BindingReferences
from arbora.browser_control.browser_control_client import (
    BrowserClientMessage,
    ControlResultMessage,
)
from arbora.browser_control.browser_control_common import (
    CancellationEffect,
    LangfuseStatus,
)
from arbora.browser_control.browser_control_descriptor import BrowserToolDescriptor
from arbora.browser_control.browser_control_durability import CallIdentity
from arbora.browser_control.browser_control_port import BrowserControlConnection
from arbora.browser_control.browser_control_server import BrowserSender

_CLIENT_MESSAGE: TypeAdapter[BrowserClientMessage] = TypeAdapter(BrowserClientMessage)


class _Connection(BrowserControlConnection):
    """Validated transport adapter for one service-owned channel state."""

    def __init__(self, service: Any, state: _ChannelState) -> None:
        self._service = service
        self._state = state

    async def receive(
        self, message: BrowserClientMessage | dict[str, Any]
    ) -> CatalogRegistrationReceipt | None:
        if self._state.closed:
            raise RuntimeError("browser control channel is closed")
        candidate = (
            message.model_dump(mode="json")
            if not isinstance(message, dict) and hasattr(message, "model_dump")
            else message
        )
        if not isinstance(candidate, dict) or not {"protocol", "type"}.issubset(
            candidate
        ):
            raise ValueError("browser control message envelope is incomplete")
        try:
            parsed = _CLIENT_MESSAGE.validate_python(candidate)
        except ValidationError as exc:
            raise ValueError("invalid browser control message") from exc
        return await self._service._receive(self._state, parsed)

    async def disconnect(self, reason: str) -> None:
        await self._service._disconnect(self._state, reason)


class BrowserControlMixinState:
    """Let focused mixins call sibling methods through final composition."""

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    def _bound_active_call(self, state: _ChannelState, call_id: str) -> _ActiveCall:
        active = self._active_calls.get(call_id)
        if active is None or active.channel is not state:
            raise PermissionError("browser message is not bound to this channel")
        return active

    @staticmethod
    def _receipt(
        active: _ActiveCall,
        *,
        status: Literal[
            "pending_confirmation",
            "dispatched",
            "succeeded",
            "failed",
            "cancelled",
            "unknown",
            "denied",
        ],
        effect: CancellationEffect,
        result: Any = None,
        result_digest: str | None = None,
        error_code: str | None = None,
    ) -> BrowserCallReceipt:
        from arbora.observability.trace_ontology import trace_id

        return BrowserCallReceipt(
            call_id=active.call_id,
            lease_id=active.request.lease_id,
            status=status,
            effect=effect,
            result=result,
            result_digest=result_digest,
            error_code=error_code,
            run_trace_id=trace_id(active.run_id),
            langfuse_status=active.langfuse_status,
        )


@dataclass(slots=True)
class _ChannelState:
    binding: BrowserChannelBinding
    refs: BindingReferences
    send: BrowserSender
    catalog: dict[str, BrowserToolDescriptor] = field(default_factory=dict)
    catalog_digest: str = ""
    tool_scope_digest: str = ""
    closed: bool = False
    cleanup_task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _ActiveCall:
    channel: _ChannelState
    descriptor: BrowserToolDescriptor
    request: BrowserCallRequest
    call_id: str
    item_id: str
    run_id: str
    request_digest: str
    admission_reference: str
    authorized: bool
    policy_reference: str
    confirmation_digest: str | None
    langfuse_status: LangfuseStatus
    result_future: asyncio.Future[ControlResultMessage]
    confirm_future: asyncio.Future[None]
    cancel_future: asyncio.Future[CancellationEffect]
    terminal_future: asyncio.Future[BrowserCallReceipt]
    proposal_guard: Callable[[], bool] | None = None
    proposal_deadline: float | None = None
    proposal_provenance: dict[str, str] = field(default_factory=dict)
    claim: dict[str, Any] | None = None
    dispatched: bool = False
    timed_out: bool = False
    cancellation_requested: bool = False
    dispatch_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    finalize_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    abort_task: asyncio.Task[None] | None = None
    reaper_task: asyncio.Task[None] | None = None


def new_active_call(
    channel: _ChannelState,
    descriptor: BrowserToolDescriptor,
    request: BrowserCallRequest,
    *,
    identity: CallIdentity,
    authorized: bool,
    policy_reference: str,
    confirmation_digest: str | None,
    langfuse_status: LangfuseStatus,
) -> _ActiveCall:
    """Create one event-loop-bound volatile call state."""

    from arbora.browser_control.browser_control_proposals import (
        accepted_browser_proposal,
    )

    proposal = accepted_browser_proposal()
    loop = asyncio.get_running_loop()
    return _ActiveCall(
        channel=channel,
        descriptor=descriptor,
        request=request,
        call_id=identity.call_id,
        item_id=identity.item_id,
        run_id=identity.run_id,
        request_digest=identity.request_digest,
        admission_reference=f"browseradmission_{secrets.token_hex(16)}",
        authorized=authorized,
        policy_reference=policy_reference,
        confirmation_digest=confirmation_digest,
        langfuse_status=langfuse_status,
        result_future=loop.create_future(),
        confirm_future=loop.create_future(),
        cancel_future=loop.create_future(),
        terminal_future=loop.create_future(),
        proposal_guard=proposal.live if proposal is not None else None,
        proposal_deadline=proposal.deadline if proposal is not None else None,
        proposal_provenance=(
            {
                "proposal_reference": proposal.proposal_id,
                "proposal_digest": proposal.digest,
                "requester_agent": proposal.origin.agent_name,
                "requester_run_reference": proposal.run_ref,
            }
            if proposal is not None
            else {}
        ),
    )


__all__ = [
    "BrowserControlMixinState",
    "_ActiveCall",
    "_ChannelState",
    "_Connection",
    "new_active_call",
]
