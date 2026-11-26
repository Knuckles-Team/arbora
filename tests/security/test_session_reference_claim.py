"""Issued bearers carry the opaque session reference (ARBORA-IDENTITY-R029.4)."""

from __future__ import annotations

import pytest

from arbora.identity.engine import IdentityUnavailable, Resolution
from arbora.identity.issuer import IssuerSettings, claims_for
from arbora.security.actor_context import ActorContext, ActorType
from arbora.security.run_authority import RunRenewalPolicy, open_run_lease
from tests.identity.test_engine_resolution import resolution_value

SETTINGS = IssuerSettings(
    "https://issuer.invalid", "fixture-audience", "fixture-tenant"
)


def _claims(**kwargs: object) -> dict:
    return claims_for(
        Resolution.parse(resolution_value()),
        SETTINGS,
        source_expires_at_ms=500_000,
        now_ms=100_100,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.spec("ARBORA-IDENTITY-R029.4")
def test_claim_present_for_session_absent_for_api_key() -> None:
    session = _claims(session_ref="sess:ab12")
    assert session["session_ref"] == "sess:ab12"
    assert "session_ref" not in _claims()  # an API-key or external caller has none
    for token_like in ("a.b.c", "x" * 129, "", "has space"):
        with pytest.raises(IdentityUnavailable):
            _claims(session_ref=token_like)
    # The reference reaches the run lease through the verified bearer.
    actor = ActorContext(
        actor_id="usr:fixture",
        actor_type=ActorType.HUMAN,
        tenant_id="fixture-tenant",
        authenticated=True,
        credential_expires_at=2_000_000_000,
    )
    lease = open_run_lease(
        actor,
        policy=RunRenewalPolicy(),
        clock=lambda: 1_900_000_000.0,
        session_ref=session["session_ref"],
    )
    assert lease.session_ref == "sess:ab12"
    assert (
        open_run_lease(
            actor, policy=RunRenewalPolicy(), clock=lambda: 1_900_000_000.0
        ).session_ref
        is None
    )
