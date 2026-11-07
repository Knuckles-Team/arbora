"""The one modality-normalisation seam every prompt entry point uses.

ARBORA-OPS-R899. ``normalize(text, parts)`` returns the text the harness runs
on, with each non-text part turned into text by the handler registered for its
modality, plus the kept originals and the derivation links. Audio is one
registered handler (the fleet's speech-to-text capability); images and
documents register their own handler the same way (``register``). A part whose
modality has no handler is never a silent drop: it is kept as an attachment
and named in the prompt text (``on_unsupported="note"``, the default), and a
prompt left with no text at all is a typed ``MODALITY_UNSUPPORTED`` refusal;
``on_unsupported="refuse"`` refuses at once. Size is bounded before any
handler runs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any, Protocol

from arbora.core.settings import setting
from arbora.modality.models import (
    Attachment,
    Derivation,
    MediaPart,
    Modality,
    ModalityRefused,
    NormalizedPrompt,
)

__all__ = [
    "DEFAULT_MAX_AUDIO_DURATION_S",
    "DEFAULT_MAX_MEDIA_BYTES",
    "ModalityHandler",
    "ModalityNormalizer",
    "default_normalizer",
    "reset_default_normalizer",
]

DEFAULT_MAX_MEDIA_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_AUDIO_DURATION_S = 600.0
MAX_PARTS = 8

_LABEL = {
    "transcript": "transcribed from an audio attachment",
    "description": "described from an image attachment",
    "extraction": "extracted from a document attachment",
}


class ModalityHandler(Protocol):
    async def derive(self, part: MediaPart) -> Derivation: ...


ProvenanceRecorder = Callable[[NormalizedPrompt], Awaitable[None]]


class ModalityNormalizer:
    """Normalise prompt parts to text through per-modality handlers."""

    def __init__(
        self,
        handlers: Mapping[Modality, ModalityHandler] | None = None,
        *,
        max_bytes: int = DEFAULT_MAX_MEDIA_BYTES,
        recorder: ProvenanceRecorder | None = None,
        on_unsupported: str = "note",
    ) -> None:
        if on_unsupported not in ("note", "refuse"):
            raise ValueError("on_unsupported must be note or refuse")
        self._handlers: dict[Modality, ModalityHandler] = dict(handlers or {})
        self.max_bytes = max_bytes
        self._recorder = recorder
        self._on_unsupported = on_unsupported

    def register(self, modality: Modality, handler: ModalityHandler) -> None:
        """Add or replace the handler for one modality (images, documents, …)."""
        self._handlers[modality] = handler

    def supports(self, modality: Modality) -> bool:
        return modality in self._handlers

    def check_size(self, size_bytes: int) -> None:
        """Refuse a part over the byte limit before it is downloaded or decoded."""
        if size_bytes > self.max_bytes:
            raise ModalityRefused(
                "MEDIA_TOO_LARGE",
                f"attachment exceeds {self.max_bytes} bytes",
                limit_bytes=self.max_bytes,
            )

    async def normalize(
        self,
        text: str,
        parts: Iterable[MediaPart],
        *,
        record: bool = True,
        on_unsupported: str | None = None,
    ) -> NormalizedPrompt:
        """Normalise ``parts``; ``on_unsupported`` overrides the seam's policy."""
        strict = (on_unsupported or self._on_unsupported) == "refuse"
        parts = list(parts)
        if len(parts) > MAX_PARTS:
            raise ModalityRefused(
                "MEDIA_TOO_LARGE", f"a prompt carries at most {MAX_PARTS} attachments"
            )
        resolved: list[tuple[MediaPart, Modality, ModalityHandler | None]] = []
        for part in parts:
            modality = part.modality
            if modality is None:
                raise ModalityRefused(
                    "MODALITY_UNSUPPORTED", "text is not a media part"
                )
            self.check_size(len(part.data))
            if not part.data:
                raise ModalityRefused("MEDIA_UNREADABLE", "the attachment is empty")
            handler = self._handlers.get(modality)
            if handler is None and strict:
                raise ModalityRefused(
                    "MODALITY_UNSUPPORTED",
                    f"no handler is registered for {modality.value}",
                    modality=modality.value,
                )
            resolved.append((part, modality, handler))
        attachments: list[Attachment] = []
        derivations: list[Derivation] = []
        unread: list[Attachment] = []
        for part, modality, handler in resolved:
            attachment = Attachment(
                attachment_id=f"media:{part.digest}",
                digest=part.digest,
                media_type=part.media_type,
                modality=modality,
                size_bytes=len(part.data),
                filename=part.filename,
                source=part.source,
            )
            attachments.append(attachment)
            if handler is None:
                unread.append(attachment)
            else:
                derivations.append(await handler.derive(part))
        if not (text and text.strip()) and not derivations:
            raise ModalityRefused(
                "MODALITY_UNSUPPORTED",
                "no attachment could be read and the prompt has no text",
                modalities=sorted({a.modality.value for a in unread}),
            )
        prompt = NormalizedPrompt(
            text=compose(text, derivations, unread),
            attachments=tuple(attachments),
            derivations=tuple(derivations),
        )
        if record and self._recorder is not None and prompt.has_media:
            await self._recorder(prompt)
        return prompt


def compose(
    text: str,
    derivations: Iterable[Derivation],
    unread: Iterable[Attachment] = (),
) -> str:
    """The prompt text: the typed text, each derived text with its source, then
    a note for each attachment no handler could read."""
    lines = [text.strip()] if text and text.strip() else []
    for derivation in derivations:
        facts = [_LABEL[derivation.kind], derivation.derived_from]
        if derivation.language:
            facts.append(f"language {derivation.language}")
        if derivation.duration_s is not None:
            facts.append(f"{derivation.duration_s:.1f} s")
        lines.append(f"[{'; '.join(facts)}] {derivation.text}")
    for attachment in unread:
        name = f" {attachment.filename}" if attachment.filename else ""
        lines.append(
            f"[attachment{name} ({attachment.media_type}, {attachment.attachment_id}) "
            f"not read: no {attachment.modality.value} handler is registered]"
        )
    return "\n".join(lines)


_default: ModalityNormalizer | None = None


def default_normalizer(
    *, fleet: Any = None, recorder: ProvenanceRecorder | None = None
) -> ModalityNormalizer:
    """The process normaliser: audio through the fleet's speech-to-text tool.

    ``ARBORA_MEDIA_MAX_BYTES`` bounds every attachment and
    ``ARBORA_AUDIO_MAX_DURATION_S`` bounds audio length.
    """
    global _default
    if _default is None:
        from arbora.modality.fleet_port import NativeIntentFleet
        from arbora.modality.speech import SpeechHandler

        max_bytes = int(setting("ARBORA_MEDIA_MAX_BYTES", str(DEFAULT_MAX_MEDIA_BYTES)))
        max_duration = float(
            setting("ARBORA_AUDIO_MAX_DURATION_S", str(DEFAULT_MAX_AUDIO_DURATION_S))
        )
        _default = ModalityNormalizer(
            {
                Modality.AUDIO: SpeechHandler(
                    fleet or NativeIntentFleet(), max_duration_s=max_duration
                )
            },
            max_bytes=max_bytes,
            recorder=recorder,
        )
    return _default


def reset_default_normalizer() -> None:
    global _default
    _default = None
