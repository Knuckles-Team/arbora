"""The verified envelope carries the acting delegation (ARBORA-A2A-R001.2.2.1)."""

from __future__ import annotations

import time

import pytest

from arbora.pin_lag.medulla import PinLagUnavailable, require_delegated_identity
from arbora.security.actor_context import ActorContext, ActorType, CredentialLease
from arbora.security.request_identity import validate_carrier_claims
from arbora.security.session import GraphSession


def _session() -> GraphSession:
    actor = ActorContext(
        actor_id="svc:host",
        actor_type=ActorType.HUMAN,
        tenant_id="tenant:one",
        authenticated=True,
        credential_lease=CredentialLease(int(time.time()) + 3600),
    )
    return GraphSession(
        actor=actor,
        tenant=actor.tenant_id,
        scopes=frozenset({"identity:delegate"}),
        policy_version="policy:one",
        audience="arbora",
    )


@pytest.mark.spec("ARBORA-A2A-R001.2.2.1")
def test_the_envelope_names_the_delegation_only_when_one_is_held() -> None:
    plain = _session()
    assert "delegation_id" not in plain.engine_verified_context()
    acting = plain.with_delegation("a2a-run:task-1")
    claims = acting.engine_verified_context()
    assert claims["delegation_id"] == "a2a-run:task-1"
    validate_carrier_claims(claims)
    # The original session is untouched: acting is a copy.
    assert plain.delegation_id is None


@pytest.mark.spec("ARBORA-A2A-R001.2.2.1")
def test_the_pinned_release_refuses_naming_the_missing_claim() -> None:
    pytest.importorskip("medulla")
    with pytest.raises(PinLagUnavailable) as refused:
        require_delegated_identity()
    assert refused.value.missing == "RequestContextClaims.delegation_id"
