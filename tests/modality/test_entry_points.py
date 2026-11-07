"""ARBORA-OPS-R899.4-.7: every prompt entry point uses the one modality seam."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any, cast

import pytest

from arbora.modality import Modality, ModalityNormalizer, ModalityRefused
from arbora.modality.speech import SpeechHandler

from ._fakes import FakeFleet, wav

pytestmark = pytest.mark.asyncio


def _normalizer(fleet: FakeFleet | None = None) -> ModalityNormalizer:
    return ModalityNormalizer(
        {Modality.AUDIO: SpeechHandler(fleet or FakeFleet(), max_duration_s=60)}
    )


# ── A2A message/send and agents.run (MCP, HTTP, web/TUI uploads) ──────────


class _Router:
    async def route(self, message: Any, **_: Any) -> Any:
        return SimpleNamespace(agent_name="arbora", selected_tools=())


class _Authority:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def dispatch(self, *, message: Any, **_: Any) -> Any:
        self.messages.append(message)
        return "task"


def _audio_message(text: str | None = "answer briefly") -> dict:
    parts: list[dict] = [{"kind": "text", "text": text}] if text else []
    parts.append(
        {
            "kind": "file",
            "file": {
                "bytes": base64.b64encode(wav()).decode(),
                "mimeType": "audio/wav",
                "name": "prompt.wav",
            },
        }
    )
    return {"role": "user", "parts": parts, "messageId": "msg-1"}


@pytest.mark.spec("ARBORA-OPS-R899.5")
async def test_a2a_audio_part_runs_the_harness_on_the_transcript() -> None:
    from arbora.a2a.service import A2AService

    fleet = FakeFleet()
    authority = _Authority()
    service = A2AService(
        authority=cast(Any, authority),
        router=cast(Any, _Router()),
        modality=_normalizer(fleet),
    )
    assert (
        await service.send_message(message=_audio_message(), idempotency_key="k")
        == "task"
    )
    (message,) = authority.messages
    text = message.task_text()
    assert text.startswith(
        "answer briefly\n[transcribed from an audio attachment; media:"
    )
    assert text.endswith("what is the capital of france")
    assert message.media_files() == []
    (kept,) = message.metadata["arboraAttachments"]
    assert kept["mediaType"] == "audio/wav" and kept["name"] == "prompt.wav"
    assert kept["source"] == "a2a:msg-1#1"
    assert kept["derivation"] == "transcript:" + kept["digest"].split(":", 1)[1]
    assert "bytes" not in kept


@pytest.mark.spec("ARBORA-OPS-R899.5")
async def test_a2a_audio_without_a_capability_refuses_typed_before_dispatch() -> None:
    from arbora.a2a.service import A2AService

    authority = _Authority()
    service = A2AService(
        authority=cast(Any, authority),
        router=cast(Any, _Router()),
        modality=_normalizer(FakeFleet(items=[])),
    )
    with pytest.raises(ModalityRefused) as caught:
        await service.send_message(message=_audio_message(None), idempotency_key="k")
    assert caught.value.code == "TRANSCRIPTION_UNAVAILABLE"
    assert authority.messages == []


@pytest.mark.spec("ARBORA-OPS-R899.3")
async def test_a2a_jsonrpc_error_names_the_modality_refusal() -> None:
    import json

    from arbora.a2a.application import _application_error

    response = _application_error(
        7, ModalityRefused("TRANSCRIPTION_UNAVAILABLE", "no reachable fleet tool")
    )
    body = json.loads(bytes(response.body))
    assert response.status_code == 503 and body["error"]["code"] == -32015
    assert body["error"]["message"].startswith("TRANSCRIPTION_UNAVAILABLE")
    too_big = _application_error(7, ModalityRefused("MEDIA_TOO_LARGE", "limit"))
    assert too_big.status_code == 422


@pytest.mark.spec("ARBORA-OPS-R899.3")
async def test_agents_run_maps_a_modality_refusal_to_a_typed_operation_refusal(
    monkeypatch,
) -> None:
    import contextlib

    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.api.ops import agents
    from arbora.security import session as security_session

    monkeypatch.setattr(
        security_session, "use_session", lambda _s: contextlib.nullcontext()
    )

    class _Service:
        async def send_message(self, **_: Any) -> Any:
            raise ModalityRefused("MEDIA_TOO_LONG", "audio exceeds 60 seconds")

    context = SimpleNamespace(
        caller=SimpleNamespace(session=object()),
        services={"a2a": _Service()},
        idempotency_key="k",
    )
    op = SimpleNamespace(id="agents.run")
    with pytest.raises(OperationRefused) as caught:
        await agents.handle_agent(context, {"message": _audio_message()}, cast(Any, op))
    assert caught.value.code == "INVALID_ARGUMENT"
    assert caught.value.details["modality_refusal"] == "MEDIA_TOO_LONG"


@pytest.mark.spec("ARBORA-OPS-R899.5")
async def test_a2a_message_model_keeps_text_bounds_and_takes_media() -> None:
    from pydantic import ValidationError

    from arbora.a2a.models import A2AMessage

    parsed = A2AMessage.model_validate(_audio_message())
    assert [i for i, _ in parsed.media_files()] == [1]
    assert parsed.task_text() == "answer briefly"
    bad = _audio_message()
    bad["parts"][1]["file"]["mimeType"] = "audio/<script>"
    with pytest.raises(ValidationError):
        A2AMessage.model_validate(bad)
    big_text = {
        "role": "user",
        "messageId": "m",
        "parts": [
            {
                "kind": "file",
                "file": {
                    "bytes": base64.b64encode(b"x" * 65_537).decode(),
                    "mimeType": "text/plain",
                },
            }
        ],
    }
    with pytest.raises(ValidationError):
        A2AMessage.model_validate(big_text)


# ── SDK A2A app for the fleet agent ───────────────────────────────────────


@pytest.mark.spec("ARBORA-OPS-R899.5")
async def test_fleet_agent_history_media_becomes_transcript_text() -> None:
    from pydantic_ai import BinaryContent
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from arbora.modality.agent_input import MediaNormalizingAgent

    seen: dict[str, Any] = {}

    class _Agent:
        name = "arbora"

        async def run(self, user_prompt: Any = None, **kwargs: Any) -> str:
            seen["history"] = kwargs["message_history"]
            return "ran"

    agent = MediaNormalizingAgent(_Agent(), _normalizer())
    history = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelRequest(
            parts=[
                UserPromptPart(
                    content=[BinaryContent(data=wav(), media_type="audio/wav")]
                )
            ]
        ),
    ]
    assert await agent.run(message_history=history) == "ran"
    assert agent.name == "arbora"
    first, second = seen["history"]
    assert first.parts[0].content == "hi"
    assert isinstance(second.parts[0].content, str)
    assert second.parts[0].content.endswith("what is the capital of france")


# ── WebUI dictation ───────────────────────────────────────────────────────


@pytest.mark.spec("ARBORA-OPS-R899.7")
async def test_webui_dictation_transcribes_through_the_seam_without_provenance(
    monkeypatch,
) -> None:
    from arbora import host_bindings
    from arbora.webui_host.voice_delegation import webui_voice_delegation_helpers

    recorded: list = []

    async def recorder(prompt) -> None:
        recorded.append(prompt)

    normalizer = ModalityNormalizer(
        {Modality.AUDIO: SpeechHandler(FakeFleet(), max_duration_s=60)},
        recorder=recorder,
    )
    monkeypatch.setattr(host_bindings, "prompt_normalizer", lambda: normalizer)
    out = await webui_voice_delegation_helpers()["transcribe_voice"](
        content=wav(), content_type="audio/wav"
    )
    assert out["text"] == "what is the capital of france" and out["language"] == "en"
    assert recorded == []


@pytest.mark.spec("ARBORA-OPS-R899.5")
async def test_a2a_refuses_a_part_no_handler_reads() -> None:
    from arbora.a2a.service import A2AService

    authority = _Authority()
    service = A2AService(
        authority=cast(Any, authority),
        router=cast(Any, _Router()),
        modality=_normalizer(),
    )
    message = _audio_message()
    message["parts"][1]["file"]["mimeType"] = "image/png"
    with pytest.raises(ModalityRefused) as caught:
        await service.send_message(message=message, idempotency_key="k")
    assert caught.value.code == "MODALITY_UNSUPPORTED" and authority.messages == []
