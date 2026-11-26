"""media.transcribe routes audio to the fleet's speech-to-text tool (ARBORA-OPS-R914)."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import media
from arbora.modality.models import Derivation, ModalityRefused
from tests.api._engine_double import context

pytestmark = pytest.mark.spec("ARBORA-OPS-R914")
_WAV = b"RIFF\x00\x00\x00\x00WAVEfmt "


@pytest.fixture(autouse=True)
def _session(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import nullcontext

    monkeypatch.setattr("arbora.security.session.use_session", lambda _s: nullcontext())


class Speech:
    def __init__(self, refuse: ModalityRefused | None = None) -> None:
        self.refuse, self.seen = refuse, []

    async def transcribe(self, part: Any, language: str | None = None) -> Any:
        self.seen.append((part.media_type, part.data, language))
        if self.refuse:
            raise self.refuse
        derivation = Derivation(
            derivation_id="d", kind="transcript", derived_from="m", text="hello"
        )
        return derivation, [{"start": 0, "end": 1, "text": "hello"}]


def _ctx(speech: Speech) -> Any:
    base = context()
    base.caller.session = object()
    base.services = {"speech_transcriber": speech}
    return base


async def _run(speech: Speech, params: dict[str, Any]) -> dict[str, Any]:
    return await media.handle_transcribe(_ctx(speech), params, None)  # type: ignore[arg-type]


async def test_blob_ref_goes_to_transcription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARBORA_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "note.wav").write_bytes(_WAV)
    speech = Speech()
    reply = await _run(speech, {"blob_ref": "note.wav", "language": "en"})
    assert reply["value"] == {
        "text": "hello",
        "segments": [{"start": 0, "end": 1, "text": "hello"}],
    }
    assert len(speech.seen) == 1 and speech.seen[0][1:] == (_WAV, "en")
    assert speech.seen[0][0].startswith("audio/")
    inline = await _run(
        speech, {"content_base64": base64.b64encode(_WAV).decode(), "blob_ref": None}
    )
    assert inline["value"]["text"] == "hello"
    assert speech.seen[-1][0] == "audio/wav"


async def test_refusals_never_reach_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARBORA_WORKSPACE_ROOT", str(tmp_path))
    speech = Speech()
    with pytest.raises(OperationRefused) as escape:
        await _run(speech, {"blob_ref": "../outside.wav"})
    assert escape.value.code == "PATH_ESCAPE"
    with pytest.raises(OperationRefused) as absent:
        await _run(speech, {"blob_ref": "gone.wav"})
    assert absent.value.code == "NOT_FOUND"
    with pytest.raises(OperationRefused) as bad:
        await _run(speech, {"content_base64": "not base64!"})
    assert bad.value.code == "INVALID_ARGUMENT"
    with pytest.raises(OperationRefused) as kind:
        await _run(speech, {"content_base64": base64.b64encode(b"plain text").decode()})
    assert kind.value.code == "INVALID_ARGUMENT"
    assert speech.seen == []
    with pytest.raises(ValueError):
        media.TranscribeParams.model_validate({})


async def test_no_transcription_tool_refuses_unavailable() -> None:
    refused = Speech(ModalityRefused("TRANSCRIPTION_UNAVAILABLE", "none served"))
    with pytest.raises(OperationRefused) as unavailable:
        await _run(refused, {"content_base64": base64.b64encode(_WAV).decode()})
    assert unavailable.value.code == "UNAVAILABLE"
