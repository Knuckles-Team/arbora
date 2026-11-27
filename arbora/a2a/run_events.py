"""A run's live events as A2A stream events (ARBORA-A2A-R001.4.1).

``RunEventObserver`` is the run's stream observer. It turns the model's text
deltas and the run's tool calls and tool results into drafts and hands each one
to the one event publisher (``A2AEventLog.publish``), which redacts it and
writes it durably before it can be delivered. Nothing here keeps an event: the
only state is the not yet flushed tail of the answer text.

Answer text is flushed up to its last whitespace, so a secret that a model
streams across two deltas is never split over two events the redactor sees
apart. Tool arguments and results are redacted, then cut to a fixed size.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from arbora.api.invoke.screening import ScreenSettings, redact_result

__all__ = ["RunEventObserver"]

#: Text is flushed as one event once this many characters are ready.
FLUSH_CHARS = 512
#: Tool arguments and results are cut to this many characters.
MAX_TOOL_CHARS = 4_000

Publish = Callable[[dict[str, Any]], Awaitable[Any]]


def _bounded(value: Any, screen: ScreenSettings) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return str(redact_result(text, screen)[0])[:MAX_TOOL_CHARS]


class RunEventObserver:
    """Publish one run's text deltas, tool calls and tool results."""

    def __init__(
        self,
        publish: Publish,
        *,
        task_id: str,
        context_id: str,
        screen: ScreenSettings,
        flush_chars: int = FLUSH_CHARS,
    ) -> None:
        self._screen = screen
        self._publish = publish
        self._task_id = task_id
        self._context_id = context_id
        self._flush_chars = flush_chars
        self._pending = ""
        #: Every character already published as an answer chunk.
        self.streamed = ""

    @property
    def artifact_id(self) -> str:
        return f"{self._task_id}:reply"

    async def __call__(self, event: Any) -> None:
        kind = getattr(event, "event_kind", None)
        if kind == "part_start":
            text = getattr(event.part, "content", None)
            if getattr(event.part, "part_kind", None) == "text" and text:
                await self._text(text)
        elif kind == "part_delta":
            delta = getattr(event.delta, "content_delta", None)
            if getattr(event.delta, "part_delta_kind", None) == "text" and delta:
                await self._text(delta)
        elif kind == "function_tool_call":
            await self.flush()
            part = event.part
            await self._tool(
                "tool-call",
                part.tool_call_id,
                part.tool_name,
                {
                    "args": _bounded(
                        part.args if part.args is not None else {}, self._screen
                    )
                },
            )
        elif kind == "function_tool_result":
            await self.flush()
            part = event.part
            await self._tool(
                "tool-result",
                getattr(part, "tool_call_id", ""),
                getattr(part, "tool_name", None) or "",
                {
                    "result": _bounded(getattr(part, "content", ""), self._screen),
                    "ok": getattr(part, "part_kind", "") == "tool-return"
                    and getattr(part, "outcome", None) in (None, "success"),
                },
            )

    async def _text(self, text: str) -> None:
        self._pending += text
        if len(self._pending) >= self._flush_chars:
            await self.flush()

    async def flush(self, *, last: bool = False) -> None:
        """Publish the ready text; keep a trailing partial word unless ``last``."""
        if last:
            ready, self._pending = self._pending, ""
        else:
            cut = max(
                (self._pending.rfind(mark) for mark in (" ", "\n", "\t")), default=-1
            )
            if cut < 0:
                return
            ready, self._pending = self._pending[: cut + 1], self._pending[cut + 1 :]
        if not ready:
            return
        await self._chunk(ready, last=False)

    async def _chunk(self, text: str, *, last: bool) -> None:
        await self._publish(
            {
                "task_id": self._task_id,
                "context_id": self._context_id,
                "kind": "artifact-update",
                "state": None,
                "final": False,
                "artifact": {
                    "artifactId": self.artifact_id,
                    "name": "reply",
                    "parts": [{"kind": "text", "text": text}],
                },
                "append": bool(self.streamed),
                "last_chunk": last,
            }
        )
        self.streamed += text

    async def finish(self, reply: str | None) -> bool:
        """Close the answer artifact; ``True`` once an answer event is published.

        Streamed text that equals the reply closes with an empty last chunk; a
        reply that differs replaces the artifact whole. Without streamed text
        the whole reply is one event, as before.
        """
        await self.flush(last=True)
        if not self.streamed:
            return False
        if reply is not None and reply != self.streamed:
            self.streamed = ""
            await self._chunk(reply, last=True)
        else:
            await self._chunk("", last=True)
        return True

    async def _tool(
        self, kind: str, call_id: str, name: str, detail: dict[str, Any]
    ) -> None:
        await self._publish(
            {
                "task_id": self._task_id,
                "context_id": self._context_id,
                "kind": "status-update",
                "state": "working",
                "final": False,
                "message": {
                    "kind": "message",
                    "role": "agent",
                    "messageId": f"{self._task_id}:{kind}:{call_id}",
                    "taskId": self._task_id,
                    "contextId": self._context_id,
                    "parts": [
                        {
                            "kind": "data",
                            "data": {
                                "type": kind,
                                "toolCallId": call_id,
                                "toolName": name,
                                **detail,
                            },
                        }
                    ],
                },
            }
        )
