"""Bounded completed-text continuation using Pydantic's process-local store."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from meristem.runtime import RunRequest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai_harness.experimental.acp import InMemorySessionStore, StoredSession

from .session_policy import PrincipalContext


@dataclass
class _Binding:
    principal: PrincipalContext
    store: InMemorySessionStore = field(default_factory=InMemorySessionStore)
    predecessor: str | None = None
    current_run_id: str | None = None
    turns: int = 0
    uncertain: bool = False


class LocalConversationHistory:
    """Host-authorized text only; no disk, tool state, replay or resume authority.

    Each binding owns an existing Pydantic store so closing the binding releases
    it without relying on the experimental store's private dictionary or an
    unsupported delete operation.
    """

    def __init__(
        self,
        model: str,
        *,
        max_sessions: int = 16,
        max_turns: int = 64,
        max_bytes: int = 1_048_576,
    ) -> None:
        if any(
            type(n) is not int or n < 1 for n in (max_sessions, max_turns, max_bytes)
        ):
            raise ValueError("history bounds must be positive integers")
        self._model = model
        self._max_sessions = max_sessions
        self._max_turns = max_turns
        self._max_bytes = max_bytes
        self._bindings: dict[str, _Binding] = {}

    async def request(
        self, principal: PrincipalContext, session_id: str, text: str
    ) -> RunRequest:
        binding = self._bindings.get(session_id)
        if binding is None:
            if len(self._bindings) >= self._max_sessions:
                raise PermissionError("local history session limit")
            binding = _Binding(principal)
            self._bindings[session_id] = binding
        if binding.principal != principal or binding.uncertain:
            raise PermissionError("local history authority unavailable")
        if binding.turns >= self._max_turns:
            raise PermissionError("local history turn limit")
        stored = await binding.store.load(session_id)
        # Reserve enough space for this profile's bounded prompt/output even
        # when JSON must escape every character. Never truncate earlier turns.
        current = self._envelope(binding, stored) if stored is not None else ""
        if len(current.encode()) + 6 * (16384 + 65536) + 8192 > self._max_bytes:
            raise PermissionError("local history byte limit")
        request = RunRequest(
            task=text, model_id=self._model, metadata={"acp_session_id": session_id}
        )
        binding.current_run_id = request.run_id
        return request

    def _bound(self, request: RunRequest) -> tuple[str, _Binding]:
        if set(request.metadata) != {"acp_session_id"}:
            raise PermissionError("local history request binding unavailable")
        session_id = request.metadata["acp_session_id"]
        binding = self._bindings.get(session_id)
        if (
            binding is None
            or binding.uncertain
            or binding.current_run_id != request.run_id
        ):
            raise PermissionError("local history session unavailable")
        return session_id, binding

    @staticmethod
    def _envelope(binding: _Binding, stored: StoredSession) -> str:
        messages = []
        for message in stored.messages:
            if (
                isinstance(message, ModelRequest)
                and len(message.parts) == 1
                and isinstance(message.parts[0], UserPromptPart)
                and isinstance(message.parts[0].content, str)
            ):
                messages.append({"role": "user", "text": message.parts[0].content})
            elif (
                isinstance(message, ModelResponse)
                and len(message.parts) == 1
                and isinstance(message.parts[0], TextPart)
            ):
                messages.append({"role": "assistant", "text": message.parts[0].content})
            else:
                raise ValueError("unsupported local history content")
        return json.dumps(
            {
                "version": 1,
                "phase": "completed",
                "run_id": binding.predecessor,
                "messages": messages,
            },
            ensure_ascii=False,
        )

    async def read(self, request: RunRequest) -> str | None:
        session_id, binding = self._bound(request)
        stored = await binding.store.load(session_id)
        if stored is None:
            if binding.predecessor is not None:
                raise ValueError("local history missing")
            return None
        content = self._envelope(binding, stored)
        if len(content.encode()) > self._max_bytes:
            raise ValueError("local history byte limit")
        return content

    async def complete(self, request: RunRequest, result: dict[str, Any]) -> None:
        session_id, binding = self._bound(request)
        if (
            result.get("run_id") != request.run_id
            or result.get("status") != "completed"
            or not isinstance(result.get("output"), str)
        ):
            raise ValueError("uncommitted local turn")
        stored = await binding.store.load(session_id)
        messages = list(stored.messages) if stored is not None else []
        messages.extend(
            [
                ModelRequest(parts=[UserPromptPart(request.task)]),
                ModelResponse(parts=[TextPart(result["output"])]),
            ]
        )
        candidate = StoredSession(messages=messages)
        if len(self._envelope(binding, candidate).encode()) > self._max_bytes:
            raise ValueError("local history byte limit")
        # A failed save may have partially changed a backend. Refuse subsequent
        # continuation instead of replaying execution or guessing its outcome.
        binding.uncertain = True
        await binding.store.save(session_id, candidate)
        binding.predecessor = request.run_id
        binding.turns += 1
        binding.uncertain = False

    async def close(self, session_id: str) -> None:
        self._bindings.pop(session_id, None)
