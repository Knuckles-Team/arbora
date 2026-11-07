"""ARBORA-OPS-R899: one modality seam turns prompt media into text."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from arbora.modality import (
    Derivation,
    MediaPart,
    Modality,
    ModalityNormalizer,
    ModalityRefused,
)
from arbora.modality.fleet_port import NativeIntentFleet
from arbora.modality.provenance import provenance_records, record_provenance
from arbora.modality.speech import SpeechHandler, is_speech_tool

from ._fakes import ROUTED, TYPED, FakeFleet, transcript, wav

pytestmark = pytest.mark.asyncio


def _audio(data: bytes | None = None, media_type: str = "audio/wav") -> MediaPart:
    return MediaPart(media_type=media_type, data=data or wav(), source="test:1#0")


def _normalizer(fleet: FakeFleet, **kwargs) -> ModalityNormalizer:
    recorded: list = kwargs.pop("recorded", [])

    async def recorder(prompt) -> None:
        recorded.append(prompt)

    return ModalityNormalizer(
        {Modality.AUDIO: SpeechHandler(fleet, max_duration_s=60)},
        recorder=recorder,
        **kwargs,
    )


@pytest.mark.spec("ARBORA-OPS-R899.2")
async def test_audio_is_transcribed_by_the_capability_found_by_contract() -> None:
    fleet = FakeFleet()
    recorded: list = []
    part = _audio()
    prompt = await _normalizer(fleet, recorded=recorded).normalize("", [part])

    # The action-routed tool is skipped; the typed contract tool is called.
    assert fleet.calls[0][:2] == ("some-speech-connector", "transcribe_speech")
    arguments = fleet.calls[0][2]
    assert base64.b64decode(arguments["artifact_b64"]) == part.data
    assert arguments["digest"] == "sha256:" + hashlib.sha256(part.data).hexdigest()
    assert prompt.text.endswith("] what is the capital of france")
    assert f"media:{part.digest}" in prompt.text and "language en" in prompt.text
    (attachment,) = prompt.attachments
    (derivation,) = prompt.derivations
    assert derivation.derived_from == attachment.attachment_id == f"media:{part.digest}"
    assert attachment.source == "test:1#0" and attachment.modality is Modality.AUDIO
    assert recorded == [prompt]


@pytest.mark.spec("ARBORA-OPS-R899.2")
async def test_capability_discovery_is_by_contract_not_server_name() -> None:
    assert is_speech_tool(TYPED) and not is_speech_tool(ROUTED)
    renamed = {**TYPED, "server": "anything-else", "name": "stt"}
    assert is_speech_tool(renamed)
    no_fields = {**TYPED, "input_schema": {"properties": {"audio_file": {}}}}
    assert not is_speech_tool(no_fields)


@pytest.mark.spec("ARBORA-OPS-R899.1")
async def test_text_and_audio_compose_and_the_provider_is_cached() -> None:
    fleet = FakeFleet()
    normalizer = _normalizer(fleet)
    await normalizer.normalize("first", [_audio()])
    prompt = await normalizer.normalize("please answer", [_audio(wav(0.4))])
    assert prompt.text.splitlines()[0] == "please answer"
    assert len(fleet.searches) == 1 and len(fleet.calls) == 2


@pytest.mark.parametrize(
    ("fleet", "code"),
    [
        (FakeFleet(items=[ROUTED]), "TRANSCRIPTION_UNAVAILABLE"),
        (FakeFleet(items=[]), "TRANSCRIPTION_UNAVAILABLE"),
        (FakeFleet(search_fails=True), "TRANSCRIPTION_UNAVAILABLE"),
        (FakeFleet(call_fails=True), "TRANSCRIPTION_UNAVAILABLE"),
        (FakeFleet(result={"ok": True, "result": {}}), "TRANSCRIPTION_REFUSED"),
        (
            FakeFleet(result=json.dumps(transcript(text=""))),
            "TRANSCRIPTION_REFUSED",
        ),
        (
            FakeFleet(
                result=transcript(status="refused", refusal_code="UNSUPPORTED_MEDIA")
            ),
            "MEDIA_UNREADABLE",
        ),
        (FakeFleet(result=transcript(duration_s=600.0)), "MEDIA_TOO_LONG"),
    ],
)
@pytest.mark.spec("ARBORA-OPS-R899.3")
async def test_unreachable_or_refusing_capability_is_a_typed_refusal(
    fleet, code
) -> None:
    recorded: list = []
    with pytest.raises(ModalityRefused) as caught:
        await _normalizer(fleet, recorded=recorded).normalize("", [_audio()])
    assert caught.value.code == code
    assert caught.value.arbora_code in {"UNAVAILABLE", "INVALID_ARGUMENT"}
    assert recorded == []


@pytest.mark.spec("ARBORA-OPS-R899.3")
async def test_size_and_duration_limits_refuse_before_any_fleet_call() -> None:
    fleet = FakeFleet()
    with pytest.raises(ModalityRefused) as too_big:
        await _normalizer(fleet, max_bytes=1_000).normalize("", [_audio()])
    assert too_big.value.code == "MEDIA_TOO_LARGE"
    with pytest.raises(ModalityRefused) as too_long:
        await _normalizer(fleet).normalize("", [_audio(wav(seconds=61))])
    assert too_long.value.code == "MEDIA_TOO_LONG"
    assert fleet.searches == [] and fleet.calls == []
    with pytest.raises(ModalityRefused) as unbound_size:
        _normalizer(fleet, max_bytes=10).check_size(11)
    assert unbound_size.value.details()["limit_bytes"] == 10


class _Vision:
    def __init__(self) -> None:
        self.parts: list[MediaPart] = []

    async def derive(self, part: MediaPart) -> Derivation:
        self.parts.append(part)
        return Derivation(
            derivation_id=f"description:{part.digest}",
            kind="description",
            derived_from=f"media:{part.digest}",
            text="a red bicycle",
        )


@pytest.mark.spec("ARBORA-OPS-R899.1")
async def test_other_modalities_plug_into_the_same_seam() -> None:
    fleet = FakeFleet()
    normalizer = _normalizer(fleet)
    image = MediaPart(media_type="image/png", data=b"\x89PNG....", filename="b.png")
    pdf = MediaPart(media_type="application/pdf", data=b"%PDF-1.7", filename="a.pdf")

    # No handler: kept and named in the text, never dropped silently.
    prompt = await normalizer.normalize("look", [image, pdf])
    assert "b.png (image/png" in prompt.text and "a.pdf (application/pdf" in prompt.text
    assert [a.modality for a in prompt.attachments] == [
        Modality.IMAGE,
        Modality.DOCUMENT,
    ]
    # Nothing readable and no text: a typed refusal.
    with pytest.raises(ModalityRefused) as caught:
        await normalizer.normalize("", [image])
    assert caught.value.code == "MODALITY_UNSUPPORTED"
    # A strict seam refuses at once.
    with pytest.raises(ModalityRefused):
        await _normalizer(fleet, on_unsupported="refuse").normalize("look", [image])

    vision = _Vision()
    normalizer.register(Modality.IMAGE, vision)
    prompt = await normalizer.normalize("what is this", [image, _audio()])
    assert vision.parts == [image]
    assert "[described from an image attachment" in prompt.text
    assert "[transcribed from an audio attachment" in prompt.text


@pytest.mark.spec("ARBORA-OPS-R899.6")
async def test_provenance_links_the_transcript_to_its_audio() -> None:
    prompt = await _normalizer(FakeFleet()).normalize("", [_audio()])
    records = {
        node_id: (label, props) for node_id, label, props in provenance_records(prompt)
    }
    attachment = prompt.attachments[0].attachment_id
    transcript_id = prompt.derivations[0].derivation_id
    assert records[attachment][0] == "MediaAttachment"
    assert records[attachment][1]["source"] == "test:1#0"
    assert "data" not in records[attachment][1]
    assert records[transcript_id][0] == "Transcript"
    assert records[transcript_id][1]["transcribedFrom"] == attachment

    created: list = []

    class _Engine:
        def create_node(self, node_id, label, properties) -> bool:
            created.append((node_id, label))
            return True

    assert record_provenance(_Engine(), prompt) == 2
    assert {label for _, label in created} == {"MediaAttachment", "Transcript"}


@pytest.mark.spec("ARBORA-OPS-R899.2")
async def test_native_fleet_uses_the_governed_find_and_act_verbs() -> None:
    seen: list[tuple[str, dict]] = []

    async def find(**kwargs):
        seen.append(("find", kwargs))
        return {"ok": True, "result": {"value": {"items": [ROUTED, TYPED]}}}

    async def act(**kwargs):
        seen.append(("act", kwargs))
        return {
            "ok": True,
            "result": {"value": {"content": [{"text": json.dumps(transcript())}]}},
        }

    fleet = NativeIntentFleet({"find": find, "act": act})
    prompt = await ModalityNormalizer(
        {Modality.AUDIO: SpeechHandler(fleet, max_duration_s=60)}
    ).normalize("", [_audio()])
    assert prompt.derivations[0].text == "what is the capital of france"
    assert seen[0] == (
        "find",
        {
            "op": "fleet.catalog.search",
            "params": {
                "query": "speech-to-text transcribe speech audio",
                "kinds": ["tool"],
                "limit": 25,
            },
        },
    )
    assert seen[1][1]["op"] == "fleet.call"
    assert seen[1][1]["params"]["tool"] == "transcribe_speech"

    async def refusing(**_kwargs):
        return {"ok": False, "error": {"code": "SCOPE_REQUIRED"}}

    refused = NativeIntentFleet({"find": refusing, "act": act})
    with pytest.raises(ModalityRefused) as caught:
        await ModalityNormalizer(
            {Modality.AUDIO: SpeechHandler(refused, max_duration_s=60)}
        ).normalize("", [_audio()])
    assert caught.value.code == "TRANSCRIPTION_UNAVAILABLE"
    assert caught.value.details()["fleet_code"] == "SCOPE_REQUIRED"
