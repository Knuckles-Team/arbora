"""Preference syntax and unavailable authority, not served R016.6 acceptance."""

import pytest
from pydantic import ValidationError

from arbora.a2a.admission import (
    ModelProfileDecision,
    ModelProfilePreference,
    ModelProfileUnavailable,
    resolve_model_profile,
)


@pytest.mark.parametrize("reference", [None, "profile-one", "x" * 512])
@pytest.mark.asyncio
async def test_uncomposed_authority_never_admits_a_preference(reference):
    request = ModelProfilePreference(preferred_profile_ref=reference)
    result = await resolve_model_profile(
        None,
        request,
        tenant="t",
        principal="p",
        profiles=["profile-one"],
        feature_schema={},
    )
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


@pytest.mark.asyncio
async def test_configured_provider_is_not_permission(monkeypatch):
    monkeypatch.setenv("ARBORA_AGENT_MODEL", "configured-model")
    monkeypatch.setenv("CHAT_MODELS", '[{"id":"profile-one"}]')
    monkeypatch.setenv("MODEL_REGISTRY_PATH", "/must-not-be-opened")
    result = await resolve_model_profile(
        None,
        ModelProfilePreference(preferred_profile_ref="profile-one"),
        tenant="t",
        principal="p",
        profiles=["profile-one"],
        feature_schema={},
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


PROFILES = ["profile-one", "profile-two"]
SCHEMA = {"definition_digest": "d" * 64}


def _engine(chosen: str, *, refused: bool):
    """A fake engine that answers one Route record bound to the request."""
    import hashlib

    async def decide(request):
        preferred = request["params"][0]["value"]["value"]
        premises = (
            [
                {
                    "subject": "PREFERENCE_NOT_LEGAL",
                    "fact": f"preference_not_honoured:{preferred}",
                }
            ]
            if refused
            else []
        )
        return {
            "records": [
                {
                    "record_id": "r1",
                    "record_digest": "d1",
                    "tenant_id": request["tenant_id"],
                    "question": request["question"],
                    "caller_principal": "principal:sha256:"
                    + hashlib.sha256(b"user:alice").hexdigest(),
                    "inputs": {"params": request["params"]},
                    "outcome": {"scores": [{"option_id": chosen}]},
                    "premises": premises,
                }
            ]
        }

    return decide


@pytest.mark.spec("ARBORA-A2A-R016.6")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preferred", "chosen", "refused", "honoured"),
    [
        ("profile-two", "profile-two", False, True),
        ("profile-two", "profile-one", True, False),
    ],
)
async def test_engine_decides_the_preference(preferred, chosen, refused, honoured):
    result = await resolve_model_profile(
        _engine(chosen, refused=refused),
        ModelProfilePreference(preferred_profile_ref=preferred),
        tenant="t",
        principal="user:alice",
        profiles=PROFILES,
        feature_schema=SCHEMA,
    )
    assert result == ModelProfileDecision(profile=chosen, preference_honoured=honoured)


@pytest.mark.spec("ARBORA-A2A-R016.6")
@pytest.mark.asyncio
async def test_unbound_or_failed_decision_grants_no_profile():
    async def broken(request):
        raise RuntimeError("engine down")

    result = await resolve_model_profile(
        broken,
        ModelProfilePreference(preferred_profile_ref="profile-two"),
        tenant="t",
        principal="user:alice",
        profiles=PROFILES,
        feature_schema=SCHEMA,
    )
    assert result.status == "unavailable" and result.reason == "INVALID_DECISION"


@pytest.mark.spec("ARBORA-A2A-R016.6")
@pytest.mark.asyncio
async def test_preference_allowed_disallowed_absent():
    """Allowed is honoured, disallowed carries the engine's premise, absent asks nothing.

    The engine is a scripted ``Decide`` returning records shaped as the route family
    seals them (``premises[].fact == "preference_not_honoured:<option>"``); this proves
    Arbora's reading and refusal handling, not the engine's ranking.
    """
    asked: list[dict] = []

    def counting(engine):
        async def decide(request):
            asked.append(request)
            return await engine(request)

        return decide

    kwargs = dict(
        tenant="t", principal="user:alice", profiles=PROFILES, feature_schema=SCHEMA
    )
    allowed = await resolve_model_profile(
        counting(_engine("profile-two", refused=False)),
        ModelProfilePreference(preferred_profile_ref="profile-two"),
        **kwargs,
    )
    assert allowed == ModelProfileDecision(
        profile="profile-two", preference_honoured=True
    )
    disallowed = await resolve_model_profile(
        counting(_engine("profile-one", refused=True)),
        ModelProfilePreference(preferred_profile_ref="profile-two"),
        **kwargs,
    )
    assert disallowed == ModelProfileDecision(
        profile="profile-one", preference_honoured=False
    )
    assert len(asked) == 2
    absent = await resolve_model_profile(
        counting(_engine("profile-one", refused=False)),
        ModelProfilePreference(preferred_profile_ref=None),
        **kwargs,
    )
    assert absent.status == "unavailable" and len(asked) == 2  # no engine call
    # A code named only in free text is not the engine's premise.
    forged = _engine("profile-one", refused=False)

    async def noisy(request):
        batch = await forged(request)
        batch["records"][0]["inputs"]["note"] = "PREFERENCE_NOT_LEGAL"
        return batch

    result = await resolve_model_profile(
        noisy, ModelProfilePreference(preferred_profile_ref="profile-one"), **kwargs
    )
    assert result.preference_honoured is True
