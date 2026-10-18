"""Preference syntax and unavailable authority, not served R016.6 acceptance."""

import pytest
from pydantic import ValidationError

from arbora.a2a.admission import (
    ModelProfilePreference,
    ModelProfileUnavailable,
    refuse_model_profile_preference,
)


@pytest.mark.parametrize("reference", [None, "profile-one", "x" * 512])
def test_uncomposed_authority_never_admits_a_preference(reference):
    request = ModelProfilePreference(preferred_profile_ref=reference)
    result = refuse_model_profile_preference(request)
    assert result.model_dump() == {
        "status": "unavailable",
        "reason": "AUTHORITY_UNAVAILABLE",
    }


@pytest.mark.parametrize("reference", ["", " ", "x" * 513, "x\n", "x\x1b", 1, True, {}])
def test_invalid_preference_is_rejected(reference):
    with pytest.raises(ValidationError):
        ModelProfilePreference(preferred_profile_ref=reference)


@pytest.mark.parametrize(
    "field", ["tenant", "principal", "grant", "provider", "api_key"]
)
def test_caller_cannot_supply_authority_fields(field):
    with pytest.raises(ValidationError):
        ModelProfilePreference.model_validate({field: "untrusted"})


@pytest.mark.parametrize(
    "value",
    [
        {"status": "available"},
        {"reason": "PERMITTED"},
        {"selected_profile_ref": "profile-one"},
        {"api_key": "fixture-secret"},
    ],
)
def test_unavailable_result_cannot_carry_success_or_secrets(value):
    with pytest.raises(ValidationError):
        ModelProfileUnavailable.model_validate(value)


def test_configured_provider_is_not_permission(monkeypatch):
    monkeypatch.setenv("ARBORA_AGENT_MODEL", "configured-model")
    monkeypatch.setenv("CHAT_MODELS", '[{"id":"profile-one"}]')
    monkeypatch.setenv("MODEL_REGISTRY_PATH", "/must-not-be-opened")
    result = refuse_model_profile_preference(
        ModelProfilePreference(preferred_profile_ref="profile-one")
    )
    assert result.reason == "AUTHORITY_UNAVAILABLE"
