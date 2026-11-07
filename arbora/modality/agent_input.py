"""Normalise binary user-prompt parts before the fleet agent runs (ARBORA-OPS-R899.5).

The SDK A2A application for the fleet agent turns an A2A file part into a
pydantic-ai ``BinaryContent`` in a ``UserPromptPart`` and calls ``agent.run``.
:class:`MediaNormalizingAgent` sits in front of the agent: it replaces every
non-text ``BinaryContent`` with the modality seam's text (audio: the
transcript, with its source digest) before the model sees the history, so a
text-only model still acts on a voice prompt. A typed refusal fails the run.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from arbora.modality.models import MediaPart, modality_of

__all__ = ["MediaNormalizingAgent"]


class MediaNormalizingAgent:
    """Delegate to ``agent`` after normalising its prompt history's media parts."""

    def __init__(self, agent: Any, normalizer: Any) -> None:
        self._agent = agent
        self._normalizer = normalizer

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)

    async def _normalize_content(self, content: Any, source: str) -> Any:
        if not isinstance(content, (list, tuple)):
            return content
        from pydantic_ai import BinaryContent

        texts: list[str] = []
        parts: list[MediaPart] = []
        kept: list[Any] = []
        for index, item in enumerate(content):
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, BinaryContent) and modality_of(item.media_type):
                parts.append(
                    MediaPart(
                        media_type=item.media_type,
                        data=bytes(item.data),
                        source=f"{source}#{index}",
                    )
                )
            else:
                kept.append(item)
        if not parts:
            return content
        prompt = await self._normalizer.normalize("\n".join(texts), parts)
        return prompt.text if not kept else [prompt.text, *kept]

    async def normalize_history(self, history: list[Any] | None) -> list[Any] | None:
        if not history:
            return history
        from pydantic_ai.messages import ModelRequest, UserPromptPart

        out: list[Any] = []
        for turn, message in enumerate(history):
            if isinstance(message, ModelRequest):
                parts = []
                for index, part in enumerate(message.parts):
                    if isinstance(part, UserPromptPart):
                        content = await self._normalize_content(
                            part.content, f"a2a-agent:{turn}.{index}"
                        )
                        if content is not part.content:
                            part = dataclasses.replace(part, content=content)
                    parts.append(part)
                message = dataclasses.replace(message, parts=parts)
            out.append(message)
        return out

    async def run(self, user_prompt: Any = None, **kwargs: Any) -> Any:
        if "message_history" in kwargs:
            kwargs["message_history"] = await self.normalize_history(
                kwargs["message_history"]
            )
        if user_prompt is not None:
            user_prompt = await self._normalize_content(user_prompt, "agent-run")
        return await self._agent.run(user_prompt, **kwargs)
