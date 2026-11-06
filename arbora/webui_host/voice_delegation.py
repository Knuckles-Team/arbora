"""The host-injected voice-transcription delegation the WebUI backend delegates through.

CONCEPT:MERISTEM-ECO.mcp.webui-voice-transcription-delegation

``agent_webui.api_extensions``'s ``POST /voice/transcribe`` route never talks to a
transcription backend itself. It calls one host-supplied workspace helper,
``transcribe_voice``, and reports 501 when it is absent (GOC-07's honest-501
contract — see ``agent-webui``'s ``tests/test_destubbed_endpoints.py::
test_voice_transcribe_honest_without_whisper``). This module is that host side,
mirroring :mod:`~arbora.webui_host.mcp_delegation`'s shape.

It delegates through the modality seam (:mod:`arbora.modality`), which finds
the fleet's ``speech-to-text`` tool by contract and calls it through the
governed ``fleet.call`` path. A mic-dictation clip becomes text in a chat box,
not permanent graph evidence, so this call records no provenance.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, TypedDict

logger = logging.getLogger(__name__)

__all__ = ["WebUiVoiceDelegation", "webui_voice_delegation_helpers"]


class _TranscribeVoice(Protocol):
    def __call__(self, *, content: bytes, content_type: str) -> Any: ...


class WebUiVoiceDelegation(TypedDict):
    """The one workspace-helper key the WebUI looks up by name.

    A typed seam on purpose, matching ``WebUiMcpDelegation``: the WebUI
    resolves this with ``get_helper('transcribe_voice')`` and answers 501
    when it is missing, so a rename here must be a type error rather than a
    silently-unwired route.
    """

    transcribe_voice: _TranscribeVoice


async def _transcribe_voice(*, content: bytes, content_type: str) -> dict[str, str]:
    """Transcribe one bounded dictation clip through the modality seam.

    ARBORA-OPS-R899.7: the clip goes to the fleet's speech-to-text capability
    through the same normaliser every prompt entry point uses, so its size and
    duration limits and its typed refusals apply here too. A dictation clip
    becomes text in a chat box, so no provenance record is written
    (``record=False``); the prompt the user then sends is ordinary text.

    Raises:
        ModalityRefused: no transcription capability is reachable, or the clip
            is too large, too long or unreadable. Never returns placeholder text.
    """
    from arbora.host_bindings import prompt_normalizer
    from arbora.modality import MediaPart

    part = MediaPart(
        media_type=content_type or "audio/webm", data=content, source="webui:voice"
    )
    prompt = await prompt_normalizer().normalize("", [part], record=False)
    derivation = prompt.derivations[0]
    return {
        "text": derivation.text,
        "language": derivation.language or "",
        "transcript_id": derivation.derivation_id,
    }


def webui_voice_delegation_helpers() -> WebUiVoiceDelegation:
    """The ``transcribe_voice`` workspace helper."""
    return WebUiVoiceDelegation(transcribe_voice=_transcribe_voice)
