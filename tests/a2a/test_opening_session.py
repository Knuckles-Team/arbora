"""The A2A work item carries the run's opening session (ARBORA-IDENTITY-R026.3)."""

from __future__ import annotations

import pytest

from arbora.a2a.authority import _opening_session
from arbora.api.domain.a2a.models import A2AMessage


def _message(metadata: dict[str, object]) -> A2AMessage:
    return A2AMessage.model_validate(
        {
            "role": "user",
            "messageId": "m-1",
            "parts": [{"kind": "text", "text": "hi"}],
            "metadata": metadata,
        }
    )


@pytest.mark.spec("ARBORA-IDENTITY-R026.3")
def test_the_item_copies_exactly_the_two_session_keys() -> None:
    copied = _opening_session(
        _message(
            {
                "client_session_id": "sess-1",
                "client_principal": "person:ada",
                "agent_name": "helper",
            }
        )
    )
    assert copied == {
        "a2a_client_session_id": "sess-1",
        "a2a_client_principal": "person:ada",
    }


@pytest.mark.spec("ARBORA-IDENTITY-R026.3")
def test_empty_oversize_and_non_text_values_are_omitted() -> None:
    assert _opening_session(_message({"client_session_id": ""})) == {}
    assert _opening_session(_message({"client_principal": "x" * 257})) == {}
    assert _opening_session(_message({"client_session_id": 7})) == {}
    assert _opening_session(_message({"client_principal": "x" * 256})) == {
        "a2a_client_principal": "x" * 256
    }
