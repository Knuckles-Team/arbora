"""Application service shared by A2A, MCP, and REST transports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from arbora.core.settings import runtime_config

from .admission import ModelProfilePreference, refuse_model_profile_preference
from .authority import A2ATaskAuthority
from .history_capability import HISTORY_EXTENSION
from .models import (
    A2AAgentCapabilities,
    A2AAgentCard,
    A2AAgentExtension,
    A2AListResult,
    A2AMessage,
    A2ASkill,
    A2ATask,
)
from .routing import A2AAssemblyUnavailable, A2ARouter
from .terminal_event import A2ATerminalEventUnavailable

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

    def supports_completed_text_history(self) -> bool:
        probe = getattr(self.authority, "supports_completed_text_history", None)
        return callable(probe) and probe() is True

    async def send_message(
        self,
        *,
        message: A2AMessage | dict[str, Any],
        idempotency_key: str,
        context_budget_tokens: int | None = None,
        blocking: bool = True,
        require_completed_text_history: bool = False,
    ) -> A2ATask:
        if (
            require_completed_text_history
            and not self.supports_completed_text_history()
        ):
            raise A2AAssemblyUnavailable("completed-text history is unavailable")
        parsed = (
            message
            if isinstance(message, A2AMessage)
            else A2AMessage.model_validate(message)
        )
        attachment_size = parsed.attachment_size()
        if attachment_size:
            limit = runtime_config.max_upload_size
            if type(limit) is not int or limit < 1 or attachment_size > limit:
                raise ValueError("attachments exceed configured upload size")
        if "arboraModelProfile" in parsed.metadata:
            preference = ModelProfilePreference(
                preferred_profile_ref=parsed.metadata["arboraModelProfile"]
            )
            refusal = refuse_model_profile_preference(preference)
            raise A2AAssemblyUnavailable(refusal.reason)
        decision = await self.router.route(
            parsed, context_budget_tokens=context_budget_tokens
        )
        requirements = (
            {"require_completed_text_history": True}
            if require_completed_text_history
            else {}
        )
        return await self.authority.dispatch(
            message=parsed,
            idempotency_key=idempotency_key,
            decision=decision,
            blocking=blocking,
            **requirements,
        )

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
        return A2AAgentCard(
            name=metadata.name,
            description=metadata.description,
            version=metadata.version,
            url=endpoint_url,
            capabilities=A2AAgentCapabilities(
                extensions=[
                    A2AAgentExtension(
                        uri=HISTORY_EXTENSION,
                        description="Strict completed-text continuation or refusal; stable retained graphs only.",
                    )
                ]
                if self.supports_completed_text_history()
                else []
            ),
            skills=[
                A2ASkill(
                    id="arbora-delegate",
                    name="Governed task delegation",
                    description=(
                        "Route a text task to an authorized existing agent and "
                        "manage its durable WorkItem lifecycle."
                    ),
                    tags=["orchestration", "knowledge-graph", "unary"],
                    input_modes=["text/plain"],
                    output_modes=["text/plain"],
                )
            ],
        )
