"""Real signature verification; synthetic secret/JWKS fixtures, not native proof."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from arbora.identity.browser_broker import ConfiguredBrowserBroker
from arbora.identity.engine import IdentityUnavailable

from .test_issuer import synthetic_signing_key as synthetic_signing_key

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.5.2")


@pytest.fixture
def broker_fixture(monkeypatch, synthetic_signing_key):
    from scion_sdk.credentials import runtime_references
    from joserfc import jwt
    from joserfc.jwk import RSAKey

    from arbora.security import auth, settings

    config = SimpleNamespace(
        auth_jwt_jwks_uri="https://fixture.invalid/jwks",
        auth_jwt_issuer="https://fixture.invalid",
        auth_jwt_audience="fixture",
        kg_policy_version="fixture-policy",
        auth_jwt_algorithms=["RS256"],
    )
    monkeypatch.setattr(settings, "security_settings", lambda: config)
    key = RSAKey.import_key(synthetic_signing_key)

    async def jwks(uri):
        assert uri == config.auth_jwt_jwks_uri
        return {"keys": [key.as_dict(is_private=False)]}

    monkeypatch.setattr(auth, "_fetch_jwks", jwks)
    tokens, reads = [], []

    def secret(reference):
        reads.append(reference)
        return tokens[-1]

    monkeypatch.setattr(runtime_references, "resolve_runtime_secret_reference", secret)

    def issue(**changes):
        claims = {
            "iss": config.auth_jwt_issuer,
            "aud": "fixture",
            "sub": "svc:broker",
            "tenant_id": "fixture-tenant",
            "roles": ["identity:authenticate"],
            "scope": "identity:authenticate",
            "exp": int(time.time()) + 120,
        }
        claims.update(changes)
        tokens.append(jwt.encode({"alg": "RS256", "kid": "fixture-key"}, claims, key))

    process = {
        "principal": "system:fixture",
        "tenant": "fixture-tenant",
        "audience": "fixture",
    }
    broker = ConfiguredBrowserBroker(
        "env://FIXTURE_BROKER",
        tenant="fixture-tenant",
        audience="fixture",
        process_claims=lambda: process,
    )

    class Fixture(SimpleNamespace):
        def __repr__(self):
            return "SyntheticBrokerFixture(redacted)"

    return Fixture(
        issue=issue, broker=broker, reads=reads, tokens=tokens, process=process
    )


def test_each_call_verifies_rotated_existing_secret(broker_fixture):
    fixture = broker_fixture
    fixture.issue()
    first = asyncio.run(fixture.broker())
    fixture.issue(sub="svc:rotated")
    second = asyncio.run(fixture.broker())
    assert [first["principal"], second["principal"]] == ["svc:broker", "svc:rotated"]
    assert len(fixture.reads) == 2
    assert first["scopes"] == ["identity:authenticate"]
    assert first["tenant"] == "fixture-tenant" and first["delegation"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"sub": "system:fixture"},
        {"tenant_id": "other"},
        {"aud": "other"},
        {"aud": ["fixture", "other"]},
        {"iss": "https://other.invalid"},
        {"exp": 1},
        {"email": "human@fixture.invalid"},
        {"principal_kind": "human"},
        {"act": {"sub": "other"}},
        {"act": {}},
        {"act": None},
        {"principal_kind": "unknown"},
        {"delegation": {}},
        {"delegation": ["other"]},
        {"delegation": None},
        {"roles": ["identity:admin"], "scope": "identity:admin"},
        {"roles": ["kg:admin"], "scope": "kg:admin"},
    ],
)
def test_wrong_or_expired_broker_never_uses_process_fallback(broker_fixture, changes):
    broker_fixture.issue(**changes)
    with pytest.raises(
        IdentityUnavailable, match="configured browser broker authority unavailable"
    ):
        asyncio.run(broker_fixture.broker())


@pytest.mark.parametrize(
    "reference", ["", "raw-secret-token", "file:///tmp/key", "env://../BAD"]
)
def test_invalid_reference_is_not_resolved_or_exposed(reference):
    with pytest.raises(IdentityUnavailable) as failure:
        ConfiguredBrowserBroker(
            reference, tenant="fixture", audience="fixture", process_claims=lambda: {}
        )
    assert str(failure.value) == "existing browser broker reference required"
    assert failure.value.__cause__ is None


def test_secret_resolver_failure_is_redacted(monkeypatch, broker_fixture):
    from scion_sdk.credentials import runtime_references

    def fail(reference):
        raise ValueError("private-token-and-reference")

    monkeypatch.setattr(runtime_references, "resolve_runtime_secret_reference", fail)
    with pytest.raises(IdentityUnavailable) as failure:
        asyncio.run(broker_fixture.broker())
    assert "private" not in str(failure.value) and failure.value.__cause__ is None


def test_process_binding_change_is_checked_after_await(broker_fixture):
    broker_fixture.issue()
    broker_fixture.process["tenant"] = "other"
    with pytest.raises(IdentityUnavailable):
        asyncio.run(broker_fixture.broker())


def test_external_default_keeps_existing_behavior_but_direct_service_refuses(
    broker_fixture,
):
    from arbora.security.actor_context import ActorType
    from arbora.security.request_identity import actor_from_bearer_token

    broker_fixture.issue(email="human@fixture.invalid", act={"sub": "other"})
    token = broker_fixture.tokens[-1]
    actor = asyncio.run(actor_from_bearer_token(token))
    assert actor.actor_type == ActorType.HUMAN
    with pytest.raises(PermissionError, match="direct service bearer"):
        asyncio.run(actor_from_bearer_token(token, require_direct_service=True))
