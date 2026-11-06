"""Typed parts, derivations and refusals for the modality seam (ARBORA-OPS-R899.1).

Every Arbora entry point that takes a prompt hands its non-text parts to the
one :class:`~arbora.modality.normalizer.ModalityNormalizer`. A part becomes
text through a registered handler (audio: the fleet's ``speech-to-text``
capability); the original part is kept as an :class:`Attachment`, and the
:class:`Derivation` links the derived text to it by digest.
"""

from __future__ import annotations

import enum
import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Attachment",
    "Derivation",
    "MediaPart",
    "Modality",
    "ModalityRefused",
    "NormalizedPrompt",
    "RefusalCode",
    "modality_of",
]


class Modality(enum.StrEnum):
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"
    DOCUMENT = "document"


RefusalCode = Literal[
    "MODALITY_UNSUPPORTED",
    "TRANSCRIPTION_UNAVAILABLE",
    "TRANSCRIPTION_REFUSED",
    "MEDIA_TOO_LARGE",
    "MEDIA_TOO_LONG",
    "MEDIA_UNREADABLE",
]

#: The Arbora envelope code each modality refusal answers with.
ARBORA_CODE: dict[str, str] = {
    "MODALITY_UNSUPPORTED": "INVALID_ARGUMENT",
    "TRANSCRIPTION_UNAVAILABLE": "UNAVAILABLE",
    "TRANSCRIPTION_REFUSED": "INVALID_ARGUMENT",
    "MEDIA_TOO_LARGE": "INVALID_ARGUMENT",
    "MEDIA_TOO_LONG": "INVALID_ARGUMENT",
    "MEDIA_UNREADABLE": "INVALID_ARGUMENT",
}


class ModalityRefused(Exception):
    """A typed refusal: the prompt is not run and nothing is fabricated."""

    def __init__(self, code: RefusalCode, detail: str = "", **extra: Any) -> None:
        self.code = code
        self.detail = detail
        self.extra = extra
        super().__init__(f"{code}: {detail}" if detail else code)

    @property
    def arbora_code(self) -> str:
        return ARBORA_CODE[self.code]

    def details(self) -> dict[str, Any]:
        return {"modality_refusal": self.code, "detail": self.detail, **self.extra}

    def user_message(self) -> str:
        """A short, safe reply for a chat channel."""
        return {
            "MODALITY_UNSUPPORTED": "I can't read that kind of attachment yet.",
            "TRANSCRIPTION_UNAVAILABLE": (
                "I can't transcribe audio right now: no transcription service is reachable."
            ),
            "TRANSCRIPTION_REFUSED": "I couldn't transcribe that audio.",
            "MEDIA_TOO_LARGE": "That attachment is too large for me to process.",
            "MEDIA_TOO_LONG": "That recording is too long for me to process.",
            "MEDIA_UNREADABLE": "I couldn't download or read that attachment.",
        }[self.code] + f" ({self.code})"


def modality_of(media_type: str) -> Modality | None:
    """Classify a MIME type; ``None`` for plain text (never a media part)."""
    kind = (media_type or "").split(";")[0].strip().lower()
    if kind.startswith("audio/"):
        return Modality.AUDIO
    if kind.startswith("image/"):
        return Modality.IMAGE
    if kind.startswith("video/"):
        return Modality.VIDEO
    if kind.startswith("text/") or not kind:
        return None
    return Modality.DOCUMENT


class MediaPart(BaseModel):
    """One non-text part of an inbound prompt, with its bytes resolved."""

    model_config = ConfigDict(frozen=True)

    media_type: str
    data: bytes = Field(repr=False)
    filename: str = ""
    #: Where the original lives: an entry-point reference such as
    #: ``telegram:<chat>:<message>`` or ``a2a:<message id>#<part index>``.
    source: str = ""

    @property
    def modality(self) -> Modality | None:
        return modality_of(self.media_type)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


class Attachment(BaseModel):
    """The kept original: identity by content digest, never the bytes."""

    model_config = ConfigDict(frozen=True)

    attachment_id: str
    digest: str
    media_type: str
    modality: Modality
    size_bytes: int
    filename: str = ""
    source: str = ""


class Derivation(BaseModel):
    """Text derived from one attachment, linked to it (``transcribedFrom``)."""

    model_config = ConfigDict(frozen=True)

    derivation_id: str
    kind: Literal["transcript", "description", "extraction"]
    derived_from: str
    text: str
    language: str | None = None
    duration_s: float | None = None
    provider_server: str = ""
    provider_tool: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class NormalizedPrompt(BaseModel):
    """The prompt the harness runs on, plus the kept originals and links."""

    model_config = ConfigDict(frozen=True)

    text: str
    attachments: tuple[Attachment, ...] = ()
    derivations: tuple[Derivation, ...] = ()

    @property
    def has_media(self) -> bool:
        return bool(self.attachments)
