"""Bearer check order and the cookie rule (ARBORA-IDENTITY-R017.4, R017.6)."""

from __future__ import annotations

import pytest

from arbora.security.bearer import BearerRefusal, BearerRefused, verify_bearer
from tests.identity._bearer_fixture import JWKS, NOW, PROFILE, mint


def _refusal(token: str, **kwargs: object) -> BearerRefusal:
    with pytest.raises(BearerRefused) as caught:
        verify_bearer(token, PROFILE, NOW, keys=kwargs.pop("keys", JWKS), **kwargs)  # type: ignore[arg-type]
    return caught.value.refusal


@pytest.mark.spec("ARBORA-IDENTITY-R017.4")
def test_check_order_and_cookie_cannot_rescue_bearer() -> None:
    ok = verify_bearer(mint(), PROFILE, NOW, keys=JWKS)
    assert (ok.subject, ok.tenant, ok.client) == ("usr:ada", "tenant:one", "cli:one")
    assert ok.scopes == ("kg:read", "kg:write")
    # Several faults at once: each earlier check wins over every later one.
    everything = {
        "iss": "https://x",
        "aud": "other",
        "exp": NOW - 999,
        "tenant_id": None,
    }
    assert _refusal(mint(forged=True, **everything)) is BearerRefusal.SIGNATURE_INVALID
    assert _refusal(mint(**everything)) is BearerRefusal.ISSUER_UNKNOWN
    assert _refusal(mint(**{**everything, "iss": PROFILE.issuer})) is (
        BearerRefusal.AUDIENCE_MISMATCH
    )
    later = {"exp": NOW - 999, "tenant_id": None}
    assert _refusal(mint(**later)) is BearerRefusal.EXPIRED
    assert _refusal(mint(nbf=NOW + 999, tenant_id=None)) is BearerRefusal.NOT_YET_VALID
    assert _refusal(mint(tenant_id=None)) is BearerRefusal.TENANT_MISSING
    assert _refusal(mint(tenant_id="  ")) is BearerRefusal.TENANT_MISSING
    # The 30 s skew is fixed: 30 s past expiry is expired, 29 s is not.
    assert verify_bearer(mint(exp=NOW - 29), PROFILE, NOW, keys=JWKS)
    assert _refusal(mint(exp=NOW - 30)) is BearerRefusal.EXPIRED
    # A cookie is not an input: an invalid bearer stays refused whatever else is sent.
    assert _refusal("not-a-jwt") is BearerRefusal.SIGNATURE_INVALID
    assert _refusal("") is BearerRefusal.MISSING


@pytest.mark.spec("ARBORA-IDENTITY-R017.6")
def test_each_refusal_code_and_its_status() -> None:
    assert _refusal(mint(), keys=None) is BearerRefusal.AUTHORITY_UNAVAILABLE
    assert (
        _refusal(mint(), expected_tenant="tenant:two") is BearerRefusal.TENANT_MISMATCH
    )
    assert _refusal(mint(), revoked=lambda _claims: True) is BearerRefusal.REVOKED
    codes = {item.value for item in BearerRefusal}
    assert {
        "BEARER_MISSING",
        "BEARER_DUPLICATE_HEADER",
        "BEARER_SIGNATURE_INVALID",
        "BEARER_ISSUER_UNKNOWN",
        "BEARER_AUDIENCE_MISMATCH",
        "BEARER_TENANT_MISSING",
        "BEARER_EXPIRED",
        "BEARER_NOT_YET_VALID",
        "BEARER_REVOKED",
        "TENANT_MISMATCH",
        "AUTHORITY_UNAVAILABLE",
    } == codes
    for item in BearerRefusal:
        expected = 503 if item is BearerRefusal.AUTHORITY_UNAVAILABLE else 401
        assert BearerRefused(item).status_code == expected
    bodies = {
        tuple(BearerRefused(item).public_body.items())
        for item in BearerRefusal
        if item is not BearerRefusal.AUTHORITY_UNAVAILABLE
    }
    assert len(bodies) == 1  # one uniform 401 body: the code is never an oracle
