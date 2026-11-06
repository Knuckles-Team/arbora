"""ARBORA-OPS-R899.4: a chat voice note runs the turn on its transcript."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.messaging import planner_handler
from arbora.messaging.host_ports import bind_host_ports
from arbora.messaging.media import attachment_media_type, normalize_inbound
from arbora.messaging.models import (
    EventType,
    InboundEvent,
    MediaAttachment,
    MediaType,
    Message,
)
from arbora.messaging.service import MessagingService
from arbora.modality import Modality, ModalityNormalizer, ModalityRefused
from arbora.modality.speech import SpeechHandler

from ..modality._fakes import FakeFleet, wav
from ._fakes import FakeMessagingBackend

pytestmark = pytest.mark.asyncio


class _Service:
    def _resolve_engine(self) -> str:
        return "engine"

    def deliver_reply(self, *_args: Any) -> bool:
        return False


def _voice_event(caption: str = "", **attachment: Any) -> InboundEvent:
    voice = MediaAttachment(
        media_type=MediaType.VOICE_NOTE,
        url="https://files.invalid/voice.oga",
        auth_header={"Authorization": "Bearer t"},
        **attachment,
    )
    message = Message(
        id="m9",
        content=caption,
        channel_id="42",
        platform="telegram",
        attachments=[voice],
    )
    return InboundEvent(
        event_type=EventType.MESSAGE,
        platform="telegram",
        channel_id="42",
        user_id="7",
        message=message,
        content=caption,
    )


def _normalizer(fleet: FakeFleet) -> ModalityNormalizer:
    return ModalityNormalizer(
        {Modality.AUDIO: SpeechHandler(fleet, max_duration_s=60)}, max_bytes=1_000_000
    )


async def _handler(monkeypatch, timer):
    monkeypatch.setenv("MESSAGING_ALLOWED_SENDERS", "telegram:7")
    monkeypatch.setattr(MessagingService, "instance", lambda _engine=None: _Service())
    return await planner_handler.create_planner_handler(
        clock=timer.clock, sleep=timer.sleep
    )


@pytest.fixture
def fetched(monkeypatch) -> list[tuple[str, dict, int]]:
    seen: list[tuple[str, dict, int]] = []

    async def fetch(attachment: MediaAttachment, max_bytes: int) -> bytes:
        seen.append((attachment.url, dict(attachment.auth_header), max_bytes))
        return wav()

    from arbora.messaging import media

    monkeypatch.setattr(media, "fetch_attachment", fetch)
    return seen


async def test_voice_note_turn_runs_on_the_transcript(
    monkeypatch, timer, fetched
) -> None:
    turns: list[str] = []

    async def turn(_engine: Any, text: str, *, session: str) -> str:
        turns.append(text)
        return "Paris"

    fleet = FakeFleet()
    bind_host_ports(turn_handler=turn, prompt_normalizer=_normalizer(fleet))
    handler = await _handler(monkeypatch, timer)
    backend = FakeMessagingBackend()
    await handler(_voice_event(), backend)
    await timer.advance(3.0)
    assert len(turns) == 1 and turns[0].endswith("what is the capital of france")
    assert "[transcribed from an audio attachment; media:" in turns[0]
    assert backend.replies == [("42", "m9", "Paris")]
    # The backend's own auth header reaches the download; the bound limit applies.
    assert fetched == [
        ("https://files.invalid/voice.oga", {"Authorization": "Bearer t"}, 1_000_000)
    ]


async def test_voice_note_without_a_transcriber_gets_a_typed_reply(
    monkeypatch, timer, fetched
) -> None:
    turns: list[str] = []

    async def turn(_engine: Any, text: str, *, session: str) -> str:
        turns.append(text)
        return "never"

    bind_host_ports(
        turn_handler=turn, prompt_normalizer=_normalizer(FakeFleet(items=[]))
    )
    handler = await _handler(monkeypatch, timer)
    backend = FakeMessagingBackend()
    await handler(_voice_event(), backend)
    await timer.advance(3.0)
    assert turns == []
    (reply,) = backend.replies
    assert reply[2].endswith("(TRANSCRIPTION_UNAVAILABLE)")

    # No normaliser bound at all: the same typed refusal, and nothing downloaded.
    bind_host_ports(prompt_normalizer=None)
    fetched.clear()
    await handler(_voice_event(), backend)
    assert backend.replies[-1][2].endswith("(TRANSCRIPTION_UNAVAILABLE)")
    assert fetched == []


async def test_declared_size_over_the_limit_refuses_before_download(fetched) -> None:
    normalizer = _normalizer(FakeFleet())
    with pytest.raises(ModalityRefused) as caught:
        await normalize_inbound(_voice_event(size_bytes=5_000_000), normalizer, "")
    assert caught.value.code == "MEDIA_TOO_LARGE" and fetched == []


async def test_unlisted_sender_audio_is_never_downloaded(
    monkeypatch, timer, fetched
) -> None:
    bind_host_ports(prompt_normalizer=_normalizer(FakeFleet()))
    handler = await _handler(monkeypatch, timer)
    monkeypatch.setenv("MESSAGING_ALLOWED_SENDERS", "telegram:someone-else")
    await handler(_voice_event(), FakeMessagingBackend())
    assert fetched == []


async def test_attachment_media_types() -> None:
    assert (
        attachment_media_type(MediaAttachment(media_type=MediaType.VOICE_NOTE))
        == "audio/ogg"
    )
    assert (
        attachment_media_type(
            MediaAttachment(media_type=MediaType.FILE, filename="a.wav")
        )
        == "audio/x-wav"
        or attachment_media_type(
            MediaAttachment(media_type=MediaType.FILE, filename="a.wav")
        )
        == "audio/wav"
    )
    assert (
        attachment_media_type(
            MediaAttachment(media_type=MediaType.AUDIO, mime_type="audio/mp4")
        )
        == "audio/mp4"
    )
