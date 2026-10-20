"""Application service shared by A2A, MCP, and REST transports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .authority import A2ATaskAuthority
from .models import A2AAgentCard, A2AListResult, A2AMessage, A2ASkill, A2ATask
from .routing import A2ARouter
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

    async def send_message(
        self,
        *,
        message: A2AMessage | dict[str, Any],
        idempotency_key: str,
        context_budget_tokens: int | None = None,
        streaming: bool = False,
    ) -> A2ATask:
        parsed = (
            message
            if isinstance(message, A2AMessage)
            else A2AMessage.model_validate(message)
        )
        decision = await self.router.route(
            parsed, context_budget_tokens=context_budget_tokens
        )
        return await self._dispatch(parsed, idempotency_key, decision, streaming)

    async def _dispatch(
        self, message: A2AMessage, key: str, decision: Any, streaming: bool
    ) -> A2ATask:
        options: dict[str, Any] = {
            "message": message,
            "idempotency_key": key,
            "decision": decision,
        }
        if streaming:
            options["streaming"] = True
        return await self.authority.dispatch(**options)

    async def streaming_supported(self) -> bool:
        probe = getattr(self.authority, "streaming_supported", None)
        return callable(probe) and await probe() is True

    async def read_events(self, task_id: str, cursor: str | None) -> dict[str, Any]:
        reader = getattr(self.authority, "read_events", None)
        if not callable(reader):
            from .authority import A2AStreamingUnavailable

            raise A2AStreamingUnavailable("durable streaming is unavailable")
        result: dict[str, Any] = await reader(task_id, cursor)
        return result

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

    async def replay_terminal(
        self, task_id: str, cursor: str | None
    ) -> tuple[A2ATask | None, str]:
        reader = getattr(self.authority, "replay_terminal", None)
        if not callable(reader):
            raise A2ATerminalEventUnavailable("terminal replay is unavailable")
        result: tuple[A2ATask | None, str] = await reader(task_id, cursor)
        return result

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
