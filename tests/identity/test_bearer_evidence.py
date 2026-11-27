"""Bearer audit evidence holds fields, never token text (ARBORA-IDENTITY-R017.7)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arbora.core.settings import RuntimeConfig
from arbora.security.bearer import BearerRefusal, BearerRefused, precheck_bearer
from arbora.security.key_cache import JwksCache
from tests.identity._bearer_fixture import (
    AUDIENCE,
    ISSUER,
    JWKS,
    JWKS_URI,
    NOW,
    mint,
)


@pytest.mark.spec("ARBORA-IDENTITY-R017.7")
def test_audit_event_fields_and_no_token_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "arbora.security.settings.security_settings",
        lambda _config: SimpleNamespace(
            auth_jwt_issuer=ISSUER,
            auth_jwt_audience=AUDIENCE,
            auth_jwt_jwks_uri=JWKS_URI,
        ),
    )

    async def fetch(_keys: JwksCache, _uri: str) -> dict:
        return JWKS

    monkeypatch.setattr("arbora.security.auth.fetch_jwks", fetch)
    events: list[dict[str, str]] = []
    good, bad = mint(), mint(aud="other")
    asyncio.run(
        precheck_bearer(
            good,
            key_cache=JwksCache(RuntimeConfig()),
            emit=events.append,
            request_id="req-1",
            now=NOW,
        )
    )
    with pytest.raises(BearerRefused):
        asyncio.run(
            precheck_bearer(
                bad,
                key_cache=JwksCache(RuntimeConfig()),
                emit=events.append,
                request_id="req-2",
                now=NOW,
            )
        )
    verified, refused = events
    assert verified["source"] == "bearer" and verified["result_status"] == "verified"
    assert (verified["request_id"], verified["tenant"], verified["principal"]) == (
        "req-1",
        "tenant:one",
        "usr:ada",
    )
    assert verified["client"] == "cli:one" and verified["key_id"] == "k1"
    assert verified["issuer_profile"] and "policy_revision" in verified
    assert refused["refusal_code"] == BearerRefusal.AUDIENCE_MISMATCH.value
    blob = repr(events)
    for token in (good, bad, *good.split("."), *bad.split(".")):
        assert token not in blob
