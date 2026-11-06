"""Chat attachments enter the modality seam (ARBORA-OPS-R899.4).

A voice note, audio file, photo or document on an inbound chat message is
downloaded once (bounded by the normaliser's byte limit, with the backend's
own per-attachment auth header) and handed to the bound prompt normaliser.
The text the turn runs on is the normalised prompt text: the typed caption,
the transcript of each audio part with its source digest, and a note for any
attachment no handler reads. The bot credential in a download URL or header is
never logged and never becomes the attachment's recorded source.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Awaitable, Callable
from typing import Any

from arbora.messaging.models import InboundEvent, MediaAttachment, MediaType
from arbora.modality.models import MediaPart, Modality, ModalityRefused, modality_of

__all__ = ["attachment_media_type", "fetch_attachment", "normalize_inbound"]

Fetch = Callable[[MediaAttachment, int], Awaitable[bytes]]

_DEFAULT_TYPE = {
    MediaType.VOICE_NOTE: "audio/ogg",
    MediaType.AUDIO: "audio/mpeg",
    MediaType.IMAGE: "image/jpeg",
    MediaType.VIDEO: "video/mp4",
}


def attachment_media_type(attachment: MediaAttachment) -> str:
    """The MIME type of a chat attachment, from the platform or its kind/name."""
    if attachment.mime_type:
        return attachment.mime_type
    default = _DEFAULT_TYPE.get(attachment.media_type)
    if default:
        return default
    guessed, _ = mimetypes.guess_type(attachment.filename or "")
    return guessed or "application/octet-stream"


async def fetch_attachment(attachment: MediaAttachment, max_bytes: int) -> bytes:
    """Download one attachment, refusing past ``max_bytes`` without reading on."""
    import httpx

    if not attachment.url.startswith(("https://", "http://")):
        raise ModalityRefused("MEDIA_UNREADABLE", "the attachment has no download URL")
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            async with client.stream(
                "GET", attachment.url, headers=dict(attachment.auth_header)
            ) as response:
                if response.status_code != 200:
                    raise ModalityRefused(
                        "MEDIA_UNREADABLE",
                        f"the platform answered HTTP {response.status_code}",
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ModalityRefused(
                            "MEDIA_TOO_LARGE",
                            f"attachment exceeds {max_bytes} bytes",
                            limit_bytes=max_bytes,
                        )
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise ModalityRefused(
            "MEDIA_UNREADABLE", f"download failed: {type(exc).__name__}"
        ) from None
    return b"".join(chunks)


async def normalize_inbound(
    event: InboundEvent,
    normalizer: Any,
    content: str,
    *,
    fetch: Fetch | None = None,
) -> str:
    """The text one inbound message's turn runs on.

    Raises:
        ModalityRefused: an attachment is too large, unreadable, or audio
            arrived with no transcription capability bound or reachable.
    """
    attachments = list(event.message.attachments) if event.message else []
    if not attachments:
        return content
    if normalizer is None:
        if any(
            modality_of(attachment_media_type(a)) is Modality.AUDIO for a in attachments
        ):
            raise ModalityRefused(
                "TRANSCRIPTION_UNAVAILABLE", "no prompt normaliser is bound"
            )
        return content
    fetch = fetch or fetch_attachment
    message_id = event.message.id if event.message else ""
    parts: list[MediaPart] = []
    for index, attachment in enumerate(attachments):
        if attachment.size_bytes:
            normalizer.check_size(attachment.size_bytes)
        data = await fetch(attachment, normalizer.max_bytes)
        parts.append(
            MediaPart(
                media_type=attachment_media_type(attachment),
                data=data,
                filename=attachment.filename,
                source=f"{event.platform}:{event.channel_id}:{message_id}#{index}",
            )
        )
    prompt = await normalizer.normalize(content, parts)
    return prompt.text
