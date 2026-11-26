"""The bearer profile and verified bearer (ARBORA-IDENTITY-R017.3)."""

from __future__ import annotations

import pytest

from arbora.security.bearer import BearerVerificationProfile, VerifiedBearer

PROFILE = {"issuer": "https://i.example.test", "audience": "api", "key_authority": "k"}


@pytest.mark.spec("ARBORA-IDENTITY-R017.3")
def test_profile_and_verified_bearer_round_trip_and_refuse_blank_tenant() -> None:
    profile = BearerVerificationProfile(**PROFILE)
    assert profile.algorithms == ("RS256",)
    assert profile.max_lifetime_seconds == 300
    assert profile.tenant_claim == "tenant_id"
    bearer = VerifiedBearer(
        subject="usr:a", tenant="t", client="c", scopes=("a",), expires_at=1
    )
    assert bearer.session_ref is None and bearer.tenant == "t"
    with pytest.raises(ValueError, match="BEARER_TENANT_MISSING"):
        VerifiedBearer(subject="usr:a", tenant=" ", client="", scopes=(), expires_at=1)
    for bad in (
        {"algorithms": ("HS256",)},
        {"max_lifetime_seconds": 301},
        {"max_lifetime_seconds": 0},
        {"tenant_claim": ""},
        {"issuer": ""},
    ):
        with pytest.raises(ValueError, match="BEARER_PROFILE_INVALID"):
            BearerVerificationProfile(**{**PROFILE, **bad})
