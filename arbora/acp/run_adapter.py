"""Compose the received AU run plan with client reads and inert previews."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from .duplex import ClientChannel
from .session_policy import AcpRefusal, AcpRefusalReason


def available() -> bool:
    try:
        from agent_utilities.harness import hosted_run
    except ImportError:
        return False
    return callable(getattr(hosted_run, "iterate_client_run", None))


class PreviewTransport:
    """Reads use the client; mutations only present their requested effects."""

    def __init__(self, channel: ClientChannel) -> None:
        self.channel = channel

    async def call(self, method: str, params: Mapping[str, Any]) -> str:
        if method == "fs/read_text_file":
            response = await self.channel.request(method, params)
            content = response.get("content")
            if not isinstance(content, str):
                raise ValueError("Invalid read response")
            return content
        await self._preview(method, params)
        raise AcpRefusal(AcpRefusalReason.PERMISSION_REQUIRED)

    async def _preview(self, method: str, params: Mapping[str, Any]) -> None:
        await self.channel.preview(method)
        content: list[dict[str, Any]] = []
        if method == "fs/write_text_file":
            original = await self.channel.request(
                "fs/read_text_file", {"path": params["path"]}
            )
            if not isinstance(original.get("content"), str):
                raise ValueError("Invalid read response")
            content = [
                {
                    "type": "diff",
                    "path": params["path"],
                    "oldText": original["content"],
                    "newText": params["content"],
                }
            ]
        elif method != "terminal/create":
            raise AcpRefusal(AcpRefusalReason.CAPABILITY_NOT_DECLARED)
        self.channel.emit(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": self.channel.session_id,
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": uuid4().hex,
                        "title": "Preview; approval authority unavailable",
                        "status": "failed",
                        "kind": "edit" if content else "execute",
                        "rawInput": dict(params),
                        "content": content,
                        "_meta": {"previewOnly": True},
                    },
                },
            }
        )


class HostedTransport(PreviewTransport):
    async def call(self, method: str, params: Mapping[str, Any]) -> str:
        from agent_utilities import host_ports

        try:
            return await super().call(method, params)
        except AcpRefusal as exc:
            raise host_ports.ClientCapabilityRefused(exc.reason.value) from None


async def run_prompt(channel: ClientChannel, text: str) -> dict[str, str]:
    from agent_utilities.harness.hosted_run import iterate_client_run

    stop_reason = "end_turn"
    async for event in iterate_client_run(text, HostedTransport(channel)):
        if event.get("type") == "error":
            stop_reason = "refusal"
        emit_run_event(channel, event)
    return {"stopReason": stop_reason}


def emit_run_event(channel: ClientChannel, event: dict[str, Any]) -> None:
    """Forward lifecycle metadata separately from user-visible model output."""
    output = event.get("output") if event.get("type") == "node_transition" else None
    if isinstance(output, str):
        update = {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": output},
        }
        method = "session/update"
        params = {"sessionId": channel.session_id, "update": update}
    else:
        method = "arbora/run"
        params = {"sessionId": channel.session_id, "event": event}
    channel.emit({"jsonrpc": "2.0", "method": method, "params": params})
