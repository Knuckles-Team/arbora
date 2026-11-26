"""Audio transcription over the fleet's speech-to-text capability (ARBORA-OPS-R914).

``media.transcribe`` takes audio the browser uploaded as a workspace file
(``blob_ref``, a path under the workspace root) or inline (``content_base64``)
and returns the transcript. The engine calls no model, so the transcript comes
from the transcription connector the fleet serves, found by contract through
the one modality seam (``arbora.modality.speech``). Arbora keeps no audio and
no transcript.
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
from collections.abc import Mapping
from typing import Any

from pydantic import Field, model_validator

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_write_op
from arbora.api.registry import OpSpec

SCOPE = "ingest:write"
MAX_AUDIO_BYTES = 25 * 1024 * 1024
NATIVE_IDEMPOTENCY: dict[str, str] = {"media.transcribe": "fleet:call"}
_MAGIC = (
    (b"OggS", "audio/ogg"),
    (b"fLaC", "audio/flac"),
    (b"ID3", "audio/mpeg"),
    (b"\xff\xfb", "audio/mpeg"),
)


class TranscribeParams(Params):
    blob_ref: str | None = Field(default=None, min_length=1, max_length=4096)
    content_base64: str | None = Field(
        default=None, max_length=(MAX_AUDIO_BYTES // 3 + 1) * 4
    )
    media_type: str | None = Field(default=None, min_length=1, max_length=128)
    language: str | None = Field(default=None, min_length=2, max_length=16)

    @model_validator(mode="after")
    def _one_source(self) -> TranscribeParams:
        if (self.blob_ref is None) == (self.content_base64 is None):
            raise ValueError("give exactly one of blob_ref and content_base64")
        return self


class Transcript(Params):
    text: str
    segments: list[Any]


def _sniffed(data: bytes) -> str | None:
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    return next((kind for magic, kind in _MAGIC if data.startswith(magic)), None)


def _audio(params: Mapping[str, Any]) -> tuple[bytes, str]:
    """The audio bytes and media type; a path escape or a missing blob is refused."""
    media_type = params.get("media_type")
    if params.get("blob_ref") is not None:
        from arbora.api.ops.workspace_files import configured_root, resolve_under_root

        target = resolve_under_root(configured_root(), params["blob_ref"])
        if not target.is_file():
            raise OperationRefused("NOT_FOUND")
        if target.stat().st_size > MAX_AUDIO_BYTES:
            raise OperationRefused("PAYLOAD_TOO_LARGE", {"limit": MAX_AUDIO_BYTES})
        data = target.read_bytes()
        media_type = media_type or mimetypes.guess_type(target.name)[0]
    else:
        try:
            data = base64.b64decode(params["content_base64"], validate=True)
        except (binascii.Error, ValueError):
            raise OperationRefused(
                "INVALID_ARGUMENT", {"field": "content_base64"}
            ) from None
        if len(data) > MAX_AUDIO_BYTES:
            raise OperationRefused("PAYLOAD_TOO_LARGE", {"limit": MAX_AUDIO_BYTES})
    media_type = media_type or _sniffed(data)
    if not media_type or not media_type.startswith("audio/"):
        raise OperationRefused("INVALID_ARGUMENT", {"field": "media_type"})
    return data, media_type


def _speech(context: Any) -> Any:
    bound = context.services.get("speech_transcriber")
    if bound is not None:
        return bound
    from arbora.modality.fleet_port import NativeIntentFleet
    from arbora.modality.normalizer import (
        DEFAULT_MAX_AUDIO_DURATION_S,
    )
    from arbora.modality.speech import SpeechHandler

    return SpeechHandler(
        NativeIntentFleet(), max_duration_s=DEFAULT_MAX_AUDIO_DURATION_S
    )


async def handle_transcribe(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    from arbora.modality.models import MediaPart, ModalityRefused
    from arbora.security.session import use_session

    data, media_type = _audio(params)
    part = MediaPart(media_type=media_type, data=data, source="media.transcribe")
    session = context.caller.session
    if session is None:
        raise RuntimeError("media.transcribe requires a verified session")
    try:
        with use_session(session):
            derivation, segments = await _speech(context).transcribe(
                part, params.get("language")
            )
    except ModalityRefused as refusal:
        raise OperationRefused(refusal.arbora_code, refusal.details()) from None
    return {"value": {"text": derivation.text, "segments": segments}}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_write_op(
            op_id="media.transcribe",
            summary="Transcribe an uploaded audio file through the fleet",
            examples=("transcribe a voice note",),
            params=TranscribeParams,
            result=Transcript,
            handler="arbora.api.ops.media.handle_transcribe",
            scope=SCOPE,
        ),
    )


specs = operations

__all__ = ["NATIVE_IDEMPOTENCY", "operations", "specs"]
