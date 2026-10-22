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


@pytest.mark.parametrize("reference", [None, "profile-one"])
def test_service_refuses_explicit_preference_before_routing(reference):
    import asyncio
    from unittest.mock import AsyncMock, Mock

    from arbora.a2a.routing import A2AAssemblyUnavailable
    from arbora.a2a.service import A2AService

    authority = Mock(dispatch=AsyncMock())
    router = Mock(route=AsyncMock())
    service = A2AService(authority=authority, router=router)
    message = {
        "role": "user",
        "messageId": "preference-request",
        "parts": [{"kind": "text", "text": "hello"}],
        "metadata": {"arboraModelProfile": reference},
    }
    with pytest.raises(A2AAssemblyUnavailable, match="AUTHORITY_UNAVAILABLE"):
        asyncio.run(service.send_message(message=message, idempotency_key="once"))
    router.route.assert_not_called()
    authority.dispatch.assert_not_called()


def test_service_without_preference_preserves_dispatch():
    import asyncio
    from unittest.mock import AsyncMock, Mock

    from arbora.a2a.models import A2ARouteDecision
    from arbora.a2a.service import A2AService

    decision = A2ARouteDecision(agent_name="existing", selection_mode="default-agent")
    authority = Mock(dispatch=AsyncMock(return_value="existing-result"))
    router = Mock(route=AsyncMock(return_value=decision))
    result = asyncio.run(
        A2AService(authority=authority, router=router).send_message(
            message={
                "role": "user",
                "messageId": "default-request",
                "parts": [{"kind": "text", "text": "hello"}],
            },
            idempotency_key="once",
        )
    )
    assert result == "existing-result"
    authority.dispatch.assert_awaited_once()


@pytest.mark.parametrize(
    "reference,expected", [("profile-one", 503), (None, 503), (True, 400), ("", 400)]
)
def test_http_profile_admission_has_no_dispatch(reference, expected):
    from unittest.mock import AsyncMock, Mock

    from fastapi.testclient import TestClient

    from arbora.a2a.application import create_a2a_application
    from arbora.a2a.service import A2AService
    from tests.a2a.test_application import Authenticator

    authority = Mock(dispatch=AsyncMock())
    router = Mock(route=AsyncMock())
    app = create_a2a_application(
        service=A2AService(authority=authority, router=router),
        authenticator=Authenticator(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/a2a",
            headers={"Authorization": "Bearer verified", "Idempotency-Key": "once"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "messageId": "preference-request",
                        "parts": [{"kind": "text", "text": "hello"}],
                        "metadata": {"arboraModelProfile": reference},
                    }
                },
            },
        )
    assert response.status_code == expected
    assert "result" not in response.json()
    router.route.assert_not_called()
    authority.dispatch.assert_not_called()
