"""Live native-agent proposals; only the attended socket may start execution."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from agent_connector_sdk.privacy import persistence_reference

from arbora.api.contracts.session import resolve_session
from arbora.browser_control.browser_control_api import (
    BrowserCallReceipt,
    BrowserCallRequest,
    BrowserProposalRequest,
    IssueLeaseRequest,
    RevokeLeaseRequest,
)
from arbora.browser_control.browser_control_client import ControlProposalResponseMessage
from arbora.browser_control.browser_control_common import canonical_internal_json
from arbora.browser_control.browser_control_server import (
    ProposalRequestMessage,
    ProposalWithdrawnMessage,
)
from arbora.browser_control.browser_control_state import (
    BrowserControlMixinState,
    _ChannelState,
)
from arbora.browser_control.browser_control_validation import validate_json_schema
from arbora.run_origin import NativeCallOrigin, require_native_call_origin

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PendingBrowserProposal:
    proposal_id: str
    digest: str
    channel: _ChannelState
    request: BrowserProposalRequest
    origin: NativeCallOrigin
    run_ref: str
    deadline: float
    result: asyncio.Future[BrowserCallReceipt]
    accepted: bool = False
    closed: bool = False
    task: asyncio.Task[None] | None = None
    unsubscribe: Callable[[], None] | None = None

    def live(self) -> bool:
        return not self.closed and self.origin.is_live()


_ACCEPTED: contextvars.ContextVar[PendingBrowserProposal | None] = (
    contextvars.ContextVar("accepted_browser_proposal", default=None)
)


def accepted_browser_proposal() -> PendingBrowserProposal | None:
    """Internal execution provenance, never supplied in browser/tool parameters."""
    return _ACCEPTED.get()


class BrowserProposalMixin(BrowserControlMixinState):
    async def request_call(self, request: BrowserProposalRequest) -> BrowserCallReceipt:
        origin = require_native_call_origin()
        resolve_session(origin.session, required_scope="kg:write")
        channel = await self._channel_for_document(request.document_ref)
        tool = self._select_tools(channel, (request.tool_id,))[0]
        if (
            channel.refs.registration_generation != request.registration_generation
            or tool.schema_digest != request.schema_digest
        ):
            raise PermissionError("browser proposal registration changed")
        validate_json_schema(tool.input_schema, request.arguments)
        await self._verify_registration(channel)
        # Snapshot mutable JSON before exposing it to either side.
        request = BrowserProposalRequest.model_validate_json(request.model_dump_json())
        pending = self._new_proposal(origin, channel, request)
        async with self._lock:
            self._assert_proposal_live(pending)
            if any(item.channel is channel for item in self._proposals.values()):
                raise RuntimeError("browser document already has a pending proposal")
            self._proposals[pending.proposal_id] = pending
        loop = asyncio.get_running_loop()

        def revoked() -> None:
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self._withdraw_proposal(pending, "requester_cancelled")
                )
            )

        pending.unsubscribe = origin.subscribe_close(revoked)
        try:
            self._assert_proposal_live(pending)
            await channel.send(self._proposal_message(pending))
            return await asyncio.wait_for(
                asyncio.shield(pending.result), timeout=request.timeout_seconds
            )
        finally:
            await self._withdraw_proposal(pending, "request_finished")

    def _new_proposal(
        self,
        origin: NativeCallOrigin,
        channel: _ChannelState,
        request: BrowserProposalRequest,
    ) -> PendingBrowserProposal:
        proposal_id = f"proposal_{secrets.token_hex(16)}"
        run_ref = persistence_reference(
            "agent_run", origin.run_id, namespace=channel.refs.tenant_reference
        )
        deadline = min(
            self._clock() + request.timeout_seconds,
            channel.refs.attended_arm_expires_at,
            channel.refs.access_token_expires_at,
        )
        digest = (
            "sha256:"
            + hashlib.sha256(
                canonical_internal_json(
                    {
                        "proposal_id": proposal_id,
                        "request": request.model_dump(mode="json"),
                        "requester_agent": origin.agent_name,
                        "requester_run_ref": run_ref,
                        "catalog_digest": channel.catalog_digest,
                        "attended_arm": channel.refs.attended_arm_reference,
                        "expires_at": deadline,
                    }
                ).encode("utf-8")
            ).hexdigest()
        )
        return PendingBrowserProposal(
            proposal_id,
            digest,
            channel,
            request,
            origin,
            run_ref,
            deadline,
            asyncio.get_running_loop().create_future(),
        )

    def _assert_proposal_live(self, pending: PendingBrowserProposal) -> None:
        channel = pending.channel
        if not pending.live() or self._clock() >= pending.deadline:
            raise PermissionError("browser proposal requester is no longer live")
        if (
            channel.closed
            or self._channels.get(channel.binding.document_ref) is not channel
            or channel.refs.registration_generation
            != pending.request.registration_generation
        ):
            raise PermissionError("browser proposal channel changed")

    @staticmethod
    def _proposal_message(pending: PendingBrowserProposal) -> ProposalRequestMessage:
        request = pending.request
        return ProposalRequestMessage(
            proposal_id=pending.proposal_id,
            proposal_digest=pending.digest,
            registration_generation=request.registration_generation,
            tool_id=request.tool_id,
            schema_digest=request.schema_digest,
            arguments=request.arguments,
            requester_agent=pending.origin.agent_name,
            requester_run_ref=pending.run_ref,
            expires_at=int(pending.deadline),
        )

    async def _receive_proposal(
        self, channel: _ChannelState, message: ControlProposalResponseMessage
    ) -> None:
        # _receive already revalidated this actual human socket's authority.
        await self._verify_registration(channel)
        async with self._lock:
            pending = self._proposals.get(message.proposal_id)
            if pending is None or pending.channel is not channel:
                raise PermissionError("browser proposal is not bound to this channel")
            self._assert_proposal_live(pending)
            if pending.accepted or pending.digest != message.proposal_digest:
                raise PermissionError("browser proposal response is stale")
            pending.accepted = True
            if not message.accepted:
                pending.result.set_exception(
                    PermissionError("browser proposal declined")
                )
                return
            # Do not block the socket receiver: it must receive confirmation/results.
            pending.task = asyncio.create_task(self._execute_proposal(pending))

    async def _execute_proposal(self, pending: PendingBrowserProposal) -> None:
        token = _ACCEPTED.set(pending)
        try:
            result = await self._accepted_proposal_call(pending)
            if not pending.result.done():
                pending.result.set_result(result)
        except asyncio.CancelledError:
            if not pending.result.done():
                pending.result.cancel()
            raise
        except Exception as exc:
            if not pending.result.done():
                pending.result.set_exception(exc)
        finally:
            _ACCEPTED.reset(token)

    async def _accepted_proposal_call(
        self, pending: PendingBrowserProposal
    ) -> BrowserCallReceipt:
        self._assert_proposal_live(pending)
        request = pending.request
        lease_task = asyncio.create_task(
            self.issue_lease(
                IssueLeaseRequest(
                    document_ref=request.document_ref,
                    tool_ids=(request.tool_id,),
                    requested_ttl_seconds=max(1, int(pending.deadline - self._clock())),
                    attended=True,
                )
            )
        )
        try:
            lease = await asyncio.shield(lease_task)
        except asyncio.CancelledError:
            lease = await lease_task
            await self._revoke_proposal_lease(lease.lease_id)
            raise
        try:
            self._assert_proposal_live(pending)
            return await self.execute_call(
                BrowserCallRequest(
                    lease_id=lease.lease_id,
                    request_id=pending.proposal_id,
                    tool_id=request.tool_id,
                    schema_digest=request.schema_digest,
                    arguments=request.arguments,
                    timeout_seconds=max(0.001, pending.deadline - self._clock()),
                )
            )
        finally:
            await self._revoke_proposal_lease(lease.lease_id)

    async def _revoke_proposal_lease(self, lease_id: str) -> None:
        cleanup = asyncio.create_task(
            self.revoke_lease(
                RevokeLeaseRequest(lease_id=lease_id, reason="proposal_finished")
            )
        )
        try:
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
        except (PermissionError, RuntimeError):
            # Retirement/expiry already prevents dispatch. Never substitute
            # another principal to perform cleanup.
            logger.info("proposal lease cleanup unavailable after authority retirement")

    async def _withdraw_proposal(
        self, pending: PendingBrowserProposal, reason: str
    ) -> None:
        # Authority checks and cleanup run in execution child tasks. They must
        # withdraw admission without cancelling/joining the parent awaiting them.
        within_execution = accepted_browser_proposal() is pending
        async with self._lock:
            first = not pending.closed
            task = pending.task
            if first:
                pending.closed = True
                self._proposals.pop(pending.proposal_id, None)
                if pending.unsubscribe is not None:
                    pending.unsubscribe()
                if not pending.result.done():
                    pending.result.cancel()
                if task is not None and not task.done() and not within_execution:
                    task.cancel()
        # External callers join the same execution cleanup, including the requester
        # awakened by cancellation from an origin-close callback.
        if task is not None and not within_execution:
            await asyncio.gather(task, return_exceptions=True)
        if not first:
            return
        if not pending.channel.closed:
            try:
                await pending.channel.send(
                    ProposalWithdrawnMessage(
                        proposal_id=pending.proposal_id, reason=reason
                    )
                )
            except Exception as exc:
                logger.info(
                    "browser proposal withdrawal transport unavailable: %s",
                    type(exc).__name__,
                )

    async def _withdraw_channel_proposals(self, channel: _ChannelState) -> None:
        for pending in tuple(self._proposals.values()):
            if pending.channel is channel:
                await self._withdraw_proposal(pending, "browser_channel_disconnected")
