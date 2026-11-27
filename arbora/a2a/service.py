"""Application service shared by A2A, MCP, and REST transports."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from arbora.api.domain.a2a.media_refs import (
    MediaRefStore,
    resolve_media_refs,
    run_media,
)
from arbora.api.domain.a2a.models import (
    TRANSITION_PAGE_LIMIT,
    A2AAgentCapabilities,
    A2AAgentCard,
    A2AContextBudget,
    A2AListResult,
    A2AMessage,
    A2APushNotificationAuthority,
    A2APushTarget,
    A2ARouteDecision,
    A2ASkill,
    A2AStreamingAuthority,
    A2ATask,
    A2ATransitionHistoryAuthority,
)
from arbora.api.domain.a2a.parts import (
    IMAGE_TYPES,
    MediaCapability,
    MediaLimits,
    admit_file_parts,
    admit_image_capability,
    check_inline_threshold,
)
from arbora.core.settings import runtime_config

from .admission import (
    ModelProfileDecision,
    ModelProfilePreference,
    ModelProfileUnavailable,
    ToolSubsetAdmissionRequest,
    admit_tool_subset,
    priority_for,
)
from .authority import (
    A2AStreamingUnavailable,
    A2ATaskAuthority,
    A2ATransitionHistoryUnavailable,
)
from .routing import A2AAssemblyUnavailable, A2ARouter
from .terminal_event import A2ATerminalEventUnavailable
from .tool_task_card import (
    AttestationSigner,
    ToolTaskAdmitter,
    admit_tool_task,
    reject_client_authority,
    tool_task_calls,
    tool_task_extension,
)

logger = logging.getLogger(__name__)


__all__ = ["A2ACardMetadata", "A2AService"]


@runtime_checkable
class _CommittedTerminalAuthority(Protocol):
    async def get_committed_task(self, task_id: str) -> A2ATask | None: ...


@dataclass(frozen=True)
class A2ACardMetadata:
    name: str = "Arbora"
    description: str = "Governed Agent OS task delegation"
    version: str = "1.0.0"


def _caller_owner() -> str:
    """The verified caller of this request: tenant and actor, never a request field."""
    from arbora.security.session import current_session

    session = current_session()
    if session is None:
        return ""
    return f"{session.tenant}:{session.actor.actor_id}"


@dataclass(frozen=True)
class A2AService:
    authority: A2ATaskAuthority
    router: A2ARouter
    card_metadata: A2ACardMetadata = A2ACardMetadata()
    #: The modality seam; ``None`` uses the process default normaliser.
    modality: Any = None
    #: The tool-task seam (ARBORA-A2A-R002.2.4); both unset leaves it unserved.
    tool_task_admitter: ToolTaskAdmitter | None = None
    attestation_signer: AttestationSigner | None = None
    #: Sends a caller's profile preference to the engine ``Route`` question;
    #: ``None`` keeps every preference refused as unavailable.
    profile_resolver: (
        Callable[
            [ModelProfilePreference],
            Awaitable[ModelProfileDecision | ModelProfileUnavailable],
        ]
        | None
    ) = None
    #: The durable event-cursor authority (ARBORA-A2A-R001.2). The card
    #: advertises ``streaming`` only when one is wired (ARBORA-A2A-R014).
    streaming: A2AStreamingAuthority | None = None
    #: The engine transition-ledger reader (ARBORA-A2A-R011). The card advertises
    #: ``stateTransitionHistory`` only when one is wired (ARBORA-A2A-R015).
    history: A2ATransitionHistoryAuthority | None = None

    #: Reads the selected model's media capability (``AgentAssemble``);
    #: ``None`` reads as unknown, and an image then refuses ``UNAVAILABLE``.
    media_capability: (
        Callable[[A2AMessage, A2ARouteDecision], Awaitable[MediaCapability | None]]
        | None
    ) = None

    #: Stores uploaded image bytes by digest for the caller's tenant (R001.5.5);
    #: ``None`` leaves a reference refusing ``UNAVAILABLE``.
    media_refs: MediaRefStore | None = None
    #: The engine decision port for the harness configuration; without one a
    #: budgeted message refuses closed (ARBORA-A2A-R017.5).
    harness_decider: Any = None
    #: The lifetime the engine seals on the harness configuration (``valid_for_ms``,
    #: MEDULLA-DECISION-ENGINE-R128.17); ``None`` sends none and the engine seals none.
    #: Arbora holds no default lifetime.
    harness_valid_for_ms: int | None = None
    #: Reads the verified caller's principal class (ARBORA-A2A-R017.3).
    principal_kind: Callable[[], str] | None = None
    #: The durable push-notification worker (ARBORA-A2A-R013); the card
    #: advertises ``pushNotifications`` only while one is wired (R013.3).
    push: A2APushNotificationAuthority | None = None
    #: The verified caller a media reference is bound to.
    caller_owner: Callable[[], str] = _caller_owner

    async def send_message(
        self,
        *,
        message: A2AMessage | dict[str, Any],
        idempotency_key: str,
        context_budget_tokens: int | None = None,
        blocking: bool = True,
    ) -> A2ATask:
        parsed = (
            message
            if isinstance(message, A2AMessage)
            else A2AMessage.model_validate(message)
        )
        reject_client_authority(parsed)
        calls = tool_task_calls(parsed)
        if calls:
            return await admit_tool_task(
                self.tool_task_admitter,
                self.attestation_signer,
                parsed,
                calls[0],
                idempotency_key,
            )
        profile: dict[str, ModelProfileDecision] = {}
        limits = MediaLimits.from_config()
        check_inline_threshold(parsed, limits)
        parsed = await resolve_media_refs(parsed, self.media_refs, self.caller_owner())
        admit_file_parts(parsed, limits)
        attachment_size = parsed.attachment_size()
        if attachment_size:
            limit = runtime_config.max_upload_size
            if type(limit) is not int or limit < 1 or attachment_size > limit:
                raise ValueError("attachments exceed configured upload size")
        if "arboraModelProfile" in parsed.metadata:
            preference = ModelProfilePreference(
                preferred_profile_ref=parsed.metadata["arboraModelProfile"]
            )
            outcome = (
                await self.profile_resolver(preference)
                if self.profile_resolver is not None
                else ModelProfileUnavailable()
            )
            if isinstance(outcome, ModelProfileUnavailable):
                raise A2AAssemblyUnavailable(outcome.reason)
            profile = {"model_profile": outcome}
        harness: dict[str, Any] = {}
        decision = await self.router.route(
            parsed, context_budget_tokens=context_budget_tokens
        )
        if context_budget_tokens is not None:
            harness = await self._admit(decision, context_budget_tokens)
        media: dict[str, Any] = {}
        if any(file.mime_type in IMAGE_TYPES for _, file in parsed.media_files()):
            capability = (
                await self.media_capability(parsed, decision)
                if self.media_capability is not None
                else None
            )
            admit_image_capability(
                parsed,
                capability,
                request_revision=str(parsed.metadata.get("arboraModelRevision") or "")
                or None,
            )
            if capability is not None:
                media = run_media(parsed, capability)
        # The model-accepted images stay on the message for the run; the rest of
        # the media goes through the modality seam (ARBORA-OPS-R899.5).
        if parsed.media_files(exclude=IMAGE_TYPES):
            parsed = await self._normalize_media(parsed)
        return await self.authority.dispatch(
            message=parsed,
            idempotency_key=idempotency_key,
            decision=decision,
            blocking=blocking,
            media=media or None,
            **profile,
            **harness,
        )

    async def _admit(
        self, decision: A2ARouteDecision, budget_tokens: int
    ) -> dict[str, Any]:
        """Admit the engine's harness decision for a budgeted message (R017.5).

        A refusal raises before ``authority.dispatch``; an admission is handed to
        it as ``harness``. A message with no budget never reaches this.
        """
        from types import SimpleNamespace

        from arbora.security.session import current_session

        if self.harness_decider is None:
            raise A2AAssemblyUnavailable(
                "harness admission is not available", "HARNESS_CONFIG_UNAVAILABLE"
            )
        if not decision.model_profile_ref or not decision.task_class:
            raise A2AAssemblyUnavailable(
                "capability record lacks model profile or task class",
                "HARNESS_CONFIG_UNAVAILABLE",
            )
        session = current_session()
        kind = self.principal_kind() if self.principal_kind else ""
        outcome = await admit_tool_subset(
            ToolSubsetAdmissionRequest(
                agent_graph_ref=decision.agent_name,
                context_budget=A2AContextBudget(tokens=budget_tokens),
                model_profile_ref=decision.model_profile_ref,
                task_class=decision.task_class,
                tenant=str(getattr(session, "tenant", "")),
                priority=priority_for(SimpleNamespace(principal_kind=kind)),
                tool_refs=decision.tool_refs,
                valid_for_ms=self.harness_valid_for_ms,
            ),
            self.harness_decider,
        )
        if not outcome.admitted:
            raise A2AAssemblyUnavailable(outcome.provenance, outcome.refusal_reason)
        return {"harness": outcome}

    async def _normalize_media(self, message: A2AMessage) -> A2AMessage:
        """Route media parts through the modality seam (ARBORA-OPS-R899.5).

        Raises:
            ModalityRefused: no transcription capability is reachable, or a
                part is too large, too long or unreadable.
        """
        from arbora.modality import MediaPart, default_normalizer

        normalizer = self.modality or default_normalizer()
        parts = [
            MediaPart(
                media_type=file.mime_type,
                data=file.decoded_bytes(),
                filename=file.name or "",
                source=f"a2a:{message.message_id}#{index}",
            )
            for index, file in message.media_files(exclude=IMAGE_TYPES)
        ]
        # A machine caller gets a typed refusal for a part no handler reads,
        # never a silently ignored attachment.
        prompt = await normalizer.normalize(
            message.task_text(), parts, on_unsupported="refuse"
        )
        links = {d.derived_from: d.derivation_id for d in prompt.derivations}
        attachments = [
            {
                "id": attachment.attachment_id,
                "digest": f"sha256:{attachment.digest}",
                "mediaType": attachment.media_type,
                "sizeBytes": attachment.size_bytes,
                "name": attachment.filename,
                "source": attachment.source,
                "derivation": links.get(attachment.attachment_id, ""),
            }
            for attachment in prompt.attachments
        ]
        return message.with_normalized_text(prompt.text, attachments)

    async def get_task(
        self,
        task_id: str,
        *,
        include_committed_event: bool = False,
        include_transition_history: bool = False,
        history_cursor: str | None = None,
        history_limit: int = TRANSITION_PAGE_LIMIT,
    ) -> A2ATask | None:
        if include_committed_event:
            if not isinstance(self.authority, _CommittedTerminalAuthority):
                raise A2ATerminalEventUnavailable(
                    "committed terminal event is unavailable"
                )
            task = await self.authority.get_committed_task(task_id)
        else:
            task = await self.authority.get(task_id)
        if task is None or not include_transition_history:
            return task
        return await self._with_history(task, task_id, history_cursor, history_limit)

    async def _with_history(
        self, task: A2ATask, task_id: str, cursor: str | None, limit: int
    ) -> A2ATask:
        """Attach one ledger page; an absent or failing authority refuses (R012.3)."""
        if self.history is None:
            raise A2ATransitionHistoryUnavailable()
        try:
            page = await self.history.transition_history(
                task_id, cursor=cursor, limit=limit
            )
        except A2ATransitionHistoryUnavailable:
            raise
        except Exception:  # the refusal never echoes the failure text
            logger.warning("with history failed", exc_info=True)
            raise A2ATransitionHistoryUnavailable() from None
        if page is None:
            raise A2ATransitionHistoryUnavailable()
        task.transition_history = page.entries
        task.transition_history_next_cursor = page.next_cursor
        return task

    async def list_tasks(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> A2AListResult:
        tasks, next_cursor = await self.authority.list(cursor=cursor, limit=limit)
        return A2AListResult(tasks=tasks, next_cursor=next_cursor)

    async def set_push_target(
        self, task_id: str, url: str, token_ref: str | None = None
    ) -> A2APushTarget:
        """Register one push webhook on the caller's task (ARBORA-A2A-R013.1)."""
        if self.push is None:
            raise A2AStreamingUnavailable("push notifications are not available")
        return await asyncio.to_thread(
            self.push.register_push_target, task_id, url, token_ref
        )

    async def cancel_task(self, task_id: str) -> A2ATask:
        return await self.authority.cancel(task_id)

    def _serving_history(self) -> A2ATransitionHistoryAuthority | None:
        """The wired history reader, only while its engine ledger read is served.

        The card never advertises a capability the pinned engine would refuse.
        """
        serves = getattr(self.history, "serves", None)
        if self.history is None or (callable(serves) and not serves()):
            return None
        return self.history

    def agent_card(self, endpoint_url: str) -> A2AAgentCard:
        metadata = self.card_metadata
        served = self.tool_task_admitter is not None and self.attestation_signer
        capabilities = A2AAgentCapabilities.from_wired_authorities(
            streaming_authority=self.streaming,
            push_notification_authority=self.push,
            transition_history_authority=self._serving_history(),
        ).model_copy(update={"extensions": [tool_task_extension()] if served else []})
        return A2AAgentCard(
            capabilities=capabilities,
            name=metadata.name,
            description=metadata.description,
            version=metadata.version,
            url=endpoint_url,
            media=MediaLimits.from_config().card(),
            skills=[
                A2ASkill(
                    id="arbora-delegate",
                    name="Governed task delegation",
                    description=(
                        "Route a text task to an authorized existing agent and "
                        "manage its durable WorkItem lifecycle."
                    ),
                    tags=["orchestration", "knowledge-graph", "unary"],
                    input_modes=["text/plain", "audio/*"],
                    output_modes=["text/plain"],
                )
            ],
        )
