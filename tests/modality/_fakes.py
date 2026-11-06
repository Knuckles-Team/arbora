"""Doubles for the modality seam: a fleet serving the speech-to-text contract."""

from __future__ import annotations

import io
import wave
from collections.abc import Mapping
from typing import Any

from arbora.modality.fleet_port import FleetCallFailed

SPEECH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifact_b64": {"type": "string"},
        "digest": {"type": "string"},
        "media_type": {"type": "string"},
        "language": {"type": "string"},
    },
}

#: An action-routed tool carrying the same fields: not the contract.
ROUTED: dict[str, Any] = {
    "name": "transcribe_media",
    "server": "audio-transcriber-mcp",
    "description": "speech-to-text sidecar",
    "input_schema": {
        "properties": {**SPEECH_SCHEMA["properties"], "action": {"type": "string"}}
    },
}
#: The typed contract tool, on a server whose name the seam never hard-codes.
TYPED: dict[str, Any] = {
    "name": "transcribe_speech",
    "server": "some-speech-connector",
    "description": "Capability: speech-to-text. Transcribe one payload.",
    "input_schema": SPEECH_SCHEMA,
}


def wav(seconds: float = 0.5, rate: int = 16_000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def transcript(text: str = "what is the capital of france", **extra: Any) -> dict:
    return {
        "schema_version": "speech-transcript/1",
        "status": "ok",
        "text": text,
        "language": "en",
        "language_probability": 0.95,
        "duration_s": 2.9,
        "segments": [{"start_ms": 0, "end_ms": 2900, "text": text}],
        "diarization": {"requested": False, "supported": False},
        **extra,
    }


class FakeFleet:
    """A :class:`FleetPort` with a scripted catalog and tool result."""

    def __init__(
        self,
        items: list[dict] | None = None,
        result: Any = None,
        *,
        search_fails: bool = False,
        call_fails: bool = False,
    ) -> None:
        self.items = [ROUTED, TYPED] if items is None else items
        self.result = (
            {"ok": True, "result": {"value": {"structured_content": transcript()}}}
            if result is None
            else result
        )
        self.search_fails = search_fails
        self.call_fails = call_fails
        self.searches: list[str] = []
        self.calls: list[tuple[str, str, dict]] = []

    async def search(self, query: str) -> list[dict]:
        self.searches.append(query)
        if self.search_fails:
            raise FleetCallFailed("SCOPE_REQUIRED")
        return list(self.items)

    async def call(self, server: str, tool: str, arguments: Mapping[str, Any]) -> Any:
        self.calls.append((server, tool, dict(arguments)))
        if self.call_fails:
            raise FleetCallFailed("UNAVAILABLE")
        return self.result
