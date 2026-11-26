"""Participant and assistant registration and error mapping (ARBORA-OPS-R897.7)."""

from __future__ import annotations

import json

import pytest

from arbora.api.engine_errors import ENGINE_ERRORS
from arbora.api.ops.registry_factory import get_registry

OPERATIONS = {
    "participant.put",
    "participant.get",
    "participant.list",
    "participant.member.add",
    "participant.scope.put",
    "participant.observation.set",
    "participant.turn.append",
    "participant.ask",
    "participant.card",
    "assistant.profile.put",
    "assistant.profile.get",
    "assistant.profile.list",
    "assistant.document_set.put",
    "assistant.document_set.get",
    "assistant.document_set.list",
}


@pytest.mark.spec("ARBORA-OPS-R897.7")
def test_registry_lists_every_operation_and_scope_is_registered() -> None:
    ops = {o["id"]: o for o in json.loads(get_registry().canonical)["ops"]}
    assert OPERATIONS <= set(ops)
    # The assistant reads and writes share the engine's one profile scope.
    assert ops["assistant.profile.get"]["scopes"] == ["profile:write"]
    assert ops["assistant.profile.put"]["scopes"] == ["profile:write"]


@pytest.mark.spec("ARBORA-OPS-R897.7")
def test_engine_codes_map_to_status() -> None:
    for code in (
        "PARTICIPANT_NOT_VISIBLE",
        "OBSERVER_NOT_PERMITTED",
        "SCOPE_NOT_VISIBLE",
        "PROFILE_NOT_VISIBLE",
    ):
        assert ENGINE_ERRORS[code] == (403, False)
    for code in (
        "NO_GROUNDED_CLAIMS",
        "CARD_ABSENT",
        "PROFILE_NOT_FOUND",
        "DOCUMENT_SET_NOT_FOUND",
        "MEMBERSHIP_SESSION_UNKNOWN",
        "SCOPE_NOT_FOUND",
    ):
        assert ENGINE_ERRORS[code] == (404, False)
    assert ENGINE_ERRORS["DERIVE_BACKLOG_FULL"] == (429, True)
    assert ENGINE_ERRORS["PROFILE_REVISION_CONFLICT"] == (400, False)
