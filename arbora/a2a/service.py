"""Application service shared by A2A, MCP, and REST transports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from arbora.core.settings import runtime_config

from .admission import (
    ModelProfileDecision,
    ModelProfilePreference,
    ModelProfileUnavailable,
)
from .authority import A2ATaskAuthority
from .models import (
    A2AAgentCapabilities,
    A2AAgentCard,
    A2AListResult,
    A2AMessage,
    A2ASkill,
    A2AStreamingAuthority,
    A2ATask,
)
from .parts import MediaLimits, admit_file_parts
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

__all__ = ["A2ACardMetadata", "A2AService"]


@runtime_checkable
class _CommittedTerminalAuthority(Protocol):
    async def get_committed_task(self, task_id: str) -> A2ATask | None: ...


@dataclass(frozen=True)
class A2ACardMetadata:
    name: str = "Arbora"
    description: str = "Governed Agent OS task delegation"
    version: str = "1.0.0"


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
        admit_file_parts(parsed, MediaLimits.from_config())
        attachment_size = parsed.attachment_size()
        if attachment_size:
            limit = runtime_config.max_upload_size
            if type(limit) is not int or limit < 1 or attachment_size > limit:
                raise ValueError("attachments exceed configured upload size")
        if parsed.media_files():
            parsed = await self._normalize_media(parsed)
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
        decision = await self.router.route(
            parsed, context_budget_tokens=context_budget_tokens
        )
        return await self.authority.dispatch(
            message=parsed,
            idempotency_key=idempotency_key,
            decision=decision,
            blocking=blocking,
            **profile,
        )

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
            for index, file in message.media_files()
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
        self, task_id: str, *, include_committed_event: bool = False
    ) -> A2ATask | None:
        if include_committed_event:
            if not isinstance(self.authority, _CommittedTerminalAuthority):
                raise A2ATerminalEventUnavailable(
                    "committed terminal event is unavailable"
                )
            return await self.authority.get_committed_task(task_id)
        return await self.authority.get(task_id)

    async def list_tasks(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> A2AListResult:
        tasks, next_cursor = await self.authority.list(cursor=cursor, limit=limit)
        return A2AListResult(tasks=tasks, next_cursor=next_cursor)

    async def cancel_task(self, task_id: str) -> A2ATask:
        return await self.authority.cancel(task_id)

    def agent_card(self, endpoint_url: str) -> A2AAgentCard:
        metadata = self.card_metadata
        served = self.tool_task_admitter is not None and self.attestation_signer
        capabilities = A2AAgentCapabilities.from_wired_authorities(
            streaming_authority=self.streaming
        ).model_copy(update={"extensions": [tool_task_extension()] if served else []})
        return A2AAgentCard(
            capabilities=capabilities,
            name=metadata.name,
            description=metadata.description,
            version=metadata.version,
            url=endpoint_url,
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
