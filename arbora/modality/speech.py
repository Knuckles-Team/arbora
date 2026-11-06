"""Audio parts become text through the fleet's speech-to-text capability.

ARBORA-OPS-R899.2. The handler finds the transcription tool by contract, not
by server name: a catalog tool qualifies when its input schema takes
``artifact_b64``, ``digest`` and ``media_type`` and takes no ``action``
routing argument, and its name or description names the ``speech-to-text``
capability. The audio-transcriber connector's ``transcribe_speech`` is one
such tool; any other connector that serves the same contract also qualifies.
The result is the typed ``speech-transcript/1`` payload.
"""

from __future__ import annotations

import base64
import io
import time
import wave
from collections.abc import Mapping
from typing import Any

from arbora.modality.fleet_port import FleetCallFailed, FleetPort, find_payload
from arbora.modality.models import (
    Derivation,
    MediaPart,
    ModalityRefused,
    RefusalCode,
)

__all__ = ["CAPABILITY", "SpeechHandler", "is_speech_tool", "wav_duration_s"]

CAPABILITY = "speech-to-text"
CONTRACT_FIELDS = frozenset({"artifact_b64", "digest", "media_type"})
SCHEMA_VERSION = "speech-transcript/1"
_DISCOVERY_QUERY = "speech-to-text transcribe speech audio"
_PROVIDER_REFUSALS: dict[str, RefusalCode] = {
    "MEDIA_TOO_LARGE": "MEDIA_TOO_LARGE",
    "MEDIA_TOO_LONG": "MEDIA_TOO_LONG",
    "UNSUPPORTED_MEDIA": "MEDIA_UNREADABLE",
}


def is_speech_tool(item: Mapping[str, Any]) -> bool:
    """Whether a catalog item serves the speech-to-text contract."""
    schema = item.get("input_schema") or {}
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(properties, Mapping) or not item.get("server"):
        return False
    if not CONTRACT_FIELDS <= set(properties) or "action" in properties:
        return False
    text = f"{item.get('name', '')} {item.get('description', '')}".lower()
    return CAPABILITY in text or "transcri" in str(item.get("name", "")).lower()


def wav_duration_s(data: bytes) -> float | None:
    """The duration of a RIFF/WAVE payload, or ``None`` for other containers."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    try:
        with wave.open(io.BytesIO(data)) as clip:
            rate = clip.getframerate()
            return clip.getnframes() / rate if rate else None
    except (wave.Error, EOFError):
        return None


class SpeechHandler:
    """Transcribe one audio part through the discovered fleet tool."""

    def __init__(
        self,
        fleet: FleetPort,
        *,
        max_duration_s: float,
        discovery_ttl_s: float = 300.0,
        clock: Any = time.monotonic,
    ) -> None:
        self._fleet = fleet
        self._max_duration_s = max_duration_s
        self._ttl = discovery_ttl_s
        self._clock = clock
        self._found: tuple[str, str] | None = None
        self._found_at = 0.0

    async def _provider(self) -> tuple[str, str]:
        if self._found is not None and self._clock() - self._found_at < self._ttl:
            return self._found
        try:
            items = await self._fleet.search(_DISCOVERY_QUERY)
        except FleetCallFailed as exc:
            raise ModalityRefused(
                "TRANSCRIPTION_UNAVAILABLE",
                "the fleet catalog is unavailable",
                fleet_code=exc.code,
            ) from None
        for item in items:
            if is_speech_tool(item):
                self._found = (str(item["server"]), str(item["name"]))
                self._found_at = self._clock()
                return self._found
        raise ModalityRefused(
            "TRANSCRIPTION_UNAVAILABLE",
            f"no reachable fleet tool serves the {CAPABILITY} capability",
        )

    async def derive(self, part: MediaPart) -> Derivation:
        duration = wav_duration_s(part.data)
        if duration is not None and duration > self._max_duration_s:
            raise ModalityRefused(
                "MEDIA_TOO_LONG", f"audio exceeds {self._max_duration_s:g} seconds"
            )
        server, tool = await self._provider()
        arguments = {
            "artifact_b64": base64.b64encode(part.data).decode("ascii"),
            "digest": f"sha256:{part.digest}",
            "media_type": part.media_type.split(";")[0].strip(),
        }
        try:
            envelope = await self._fleet.call(server, tool, arguments)
        except FleetCallFailed as exc:
            self._found = None  # rediscover next time
            raise ModalityRefused(
                "TRANSCRIPTION_UNAVAILABLE",
                "the transcription capability did not answer",
                fleet_code=exc.code,
            ) from None
        result = find_payload(
            envelope, lambda node: node.get("schema_version") == SCHEMA_VERSION
        )
        if result is None:
            raise ModalityRefused(
                "TRANSCRIPTION_REFUSED", "the provider returned no speech transcript"
            )
        if result.get("status") != "ok":
            code = str(result.get("refusal_code") or "TRANSCRIPTION_FAILED")
            mapped: RefusalCode = _PROVIDER_REFUSALS.get(code, "TRANSCRIPTION_REFUSED")
            raise ModalityRefused(mapped, code, provider_refusal=code)
        duration_s = result.get("duration_s")
        if isinstance(duration_s, (int, float)) and duration_s > self._max_duration_s:
            raise ModalityRefused(
                "MEDIA_TOO_LONG", f"audio exceeds {self._max_duration_s:g} seconds"
            )
        text = str(result.get("text") or "").strip()
        if not text:
            raise ModalityRefused("TRANSCRIPTION_REFUSED", "the audio held no speech")
        segments = result.get("segments") or []
        return Derivation(
            derivation_id=f"transcript:{part.digest}",
            kind="transcript",
            derived_from=f"media:{part.digest}",
            text=text,
            language=result.get("language"),
            duration_s=float(duration_s)
            if isinstance(duration_s, (int, float))
            else None,
            provider_server=server,
            provider_tool=tool,
            detail={
                "language_probability": result.get("language_probability"),
                "segments": len(segments) if isinstance(segments, list) else 0,
                "diarization": result.get("diarization"),
                "model": result.get("model", ""),
            },
        )
