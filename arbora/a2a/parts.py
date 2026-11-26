"""Admission of A2A file parts before any task exists (ARBORA-A2A-R001.5).

One limit table feeds every check here. Text and image parts are admitted
against it; audio, video and document parts belong to the modality seam
(ARBORA-OPS-R899.5) and pass through untouched. A refusal is JSON-RPC
``-32602`` with one of the closed reasons below and creates zero tasks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arbora.a2a.models import A2AFilePart, A2AMessage

IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
TEXT_TYPES = frozenset({"text/plain", "text/markdown"})
ALLOWED_TYPES = IMAGE_TYPES | TEXT_TYPES

#: At most this many file parts in one message.
MAX_FILE_PARTS = 8
#: The decoded text bytes one message may carry in total.
MAX_TEXT_BYTES = 65_536

REASONS = frozenset(
    {
        "file_part_type_unsupported",
        "file_part_too_large",
        "file_part_count",
        "file_part_type_mismatch",
        "request_media_too_large",
        "image_part_count",
    }
)


class FilePartRefused(ValueError):
    """A file part the limit table refuses; the message is the closed reason."""

    def __init__(self, reason: str) -> None:
        if reason not in REASONS:
            raise ValueError("unknown file part refusal reason")
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class MediaLimits:
    """The effective limits; the agent card publishes this same table."""

    max_file_part_bytes: int
    max_request_media_bytes: int
    max_image_parts: int
    max_file_parts: int = MAX_FILE_PARTS
    max_text_bytes: int = MAX_TEXT_BYTES

    @classmethod
    def from_config(cls, config: object | None = None) -> MediaLimits:
        """The limits from runtime settings, each bounded to its range."""
        if config is None:
            from arbora.core.settings import runtime_config

            config = runtime_config
        return cls(
            max_file_part_bytes=_bounded(
                config, "a2a_max_file_part_bytes", 5_242_880, 1_024, 33_554_432
            ),
            max_request_media_bytes=_bounded(
                config, "a2a_max_request_media_bytes", 20_971_520, 1_024, 134_217_728
            ),
            max_image_parts=_bounded(config, "a2a_max_image_parts", 4, 1, 8),
        )


def _bounded(config: object, name: str, default: int, low: int, high: int) -> int:
    value = getattr(config, name, default)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def sniff_image_type(data: bytes) -> str | None:
    """The image type the leading signature names, or ``None``."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def admit_file_parts(message: A2AMessage, limits: MediaLimits) -> None:
    """Refuse the message unless every text and image part fits the table."""
    files = [part.file for part in message.parts if isinstance(part, A2AFilePart)]
    if len(files) > limits.max_file_parts:
        raise FilePartRefused("file_part_count")
    text_bytes = image_parts = media_bytes = 0
    for file in files:
        kind = file.mime_type
        if kind.startswith(("image/", "text/")) and kind not in ALLOWED_TYPES:
            raise FilePartRefused("file_part_type_unsupported")
        if kind not in ALLOWED_TYPES:
            continue
        data = file.decoded_bytes()
        if len(data) > limits.max_file_part_bytes:
            raise FilePartRefused("file_part_too_large")
        if kind in TEXT_TYPES:
            text_bytes += len(data)
            if text_bytes > limits.max_text_bytes:
                raise FilePartRefused("file_part_too_large")
            continue
        if sniff_image_type(data) != kind:
            raise FilePartRefused("file_part_type_mismatch")
        image_parts += 1
        media_bytes += len(data)
    if image_parts > limits.max_image_parts:
        raise FilePartRefused("image_part_count")
    if media_bytes > limits.max_request_media_bytes:
        raise FilePartRefused("request_media_too_large")


class MediaCapabilityRefused(Exception):
    """An image the selected model cannot take; ``code`` is the typed refusal."""

    def __init__(self, code: str, reason: str) -> None:
        if (code, reason) not in {
            ("UNAVAILABLE", "media_capability_unknown"),
            ("POLICY_DENIED", "media_capability_stale"),
            ("POLICY_DENIED", "media_type_not_accepted"),
            ("POLICY_DENIED", "media_part_too_large"),
        }:
            raise ValueError("unknown media capability refusal")
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True, slots=True)
class MediaCapability:
    """What the selected model accepts, read from the engine's assembly answer.

    ``accepts`` holds one ``(media_type, max_bytes)`` per accepted type; the
    engine states one aggregate byte limit, so every type carries it.
    ``revision`` is the model's definition digest.
    """

    revision: str
    accepts: tuple[tuple[str, int], ...]
    max_parts: int

    def limit_for(self, media_type: str) -> int | None:
        return dict(self.accepts).get(media_type)


def capability_from_assembly(result: Any) -> MediaCapability | None:
    """Map an ``AgentAssemble`` answer to the capability; ``None`` is unknown.

    A client that predates the answer's ``model_input`` and ``model_revision``
    fields, or an answer whose ``model_input`` is null, reads as unknown, and
    unknown never admits.
    """
    model_input = getattr(result, "model_input", None)
    revision = getattr(result, "model_revision", None)
    if model_input is None or not isinstance(revision, str) or not revision:
        return None
    return MediaCapability(
        revision=revision,
        accepts=tuple(
            (str(getattr(kind, "value", kind)), int(model_input.max_aggregate_bytes))
            for kind in model_input.media_types
        ),
        max_parts=int(model_input.max_parts),
    )


def admit_image_capability(
    message: A2AMessage,
    capability: MediaCapability | None,
    *,
    request_revision: str | None = None,
) -> None:
    """Refuse an image part the selected model cannot take, before any task.

    Only the model-accepted types (text, png, jpeg, webp) are checked here;
    audio, video and document parts belong to the modality seam and are never
    checked against the model capability. Text parts stay bounded by the limit
    table. ``request_revision`` is the model revision the request was made
    against; when given it must equal the capability's.
    """
    images = [
        part.file
        for part in message.parts
        if isinstance(part, A2AFilePart) and part.file.mime_type in IMAGE_TYPES
    ]
    if not images:
        return
    if capability is None:
        raise MediaCapabilityRefused("UNAVAILABLE", "media_capability_unknown")
    if request_revision is not None and request_revision != capability.revision:
        raise MediaCapabilityRefused("POLICY_DENIED", "media_capability_stale")
    if len(images) > capability.max_parts:
        raise MediaCapabilityRefused("POLICY_DENIED", "media_part_too_large")
    for file in images:
        limit = capability.limit_for(file.mime_type)
        if limit is None:
            raise MediaCapabilityRefused("POLICY_DENIED", "media_type_not_accepted")
        if len(file.decoded_bytes()) > limit:
            raise MediaCapabilityRefused("POLICY_DENIED", "media_part_too_large")


__all__ = [
    "ALLOWED_TYPES",
    "FilePartRefused",
    "MediaCapability",
    "MediaCapabilityRefused",
    "MediaLimits",
    "admit_file_parts",
    "admit_image_capability",
    "capability_from_assembly",
    "sniff_image_type",
]
