"""Scoped credential reuse models and behaviour (ARBORA-IDENTITY-R023.1, R023.2)."""

from __future__ import annotations

import asyncio

import pytest

from arbora.access.credential_use import (
    AuthorizationChallenge,
    CredentialRefusal,
    CredentialRefused,
    CredentialUseGrant,
    CredentialUseRequest,
    resolve_credential,
)
from tests.identity._credential_fixture import CALLER, NOW, TOKEN, Ports, request


@pytest.mark.spec("ARBORA-IDENTITY-R023.1")
def test_models_round_trip_and_refuse_supplied_actor() -> None:
    req = request(scopes=("b", "a", "a"))
    assert req.scopes == ("a", "b")
    built = CredentialUseRequest.from_verified(
        CALLER,
        {
            "provider": "idp",
            "audience": "aud",
            "scopes": ["s"],
            "resource": "r",
            "operation": "o",
        },
        connector="mail-mcp",
        owner="ada",
    )
    assert (built.requesting_principal, built.owner_principal) == ("bob", "ada")
    for field in ("actor", "principal", "owner", "connector", "agent"):
        with pytest.raises(CredentialRefused) as caught:
            CredentialUseRequest.from_verified(
                CALLER, {"provider": "idp", field: "x"}, connector="mail-mcp"
            )
        assert caught.value.refusal is CredentialRefusal.REQUEST_INVALID
    with pytest.raises(CredentialRefused):
        request(scopes=())
    grant = CredentialUseGrant(req, "dec", "vault:x", 1, 2, "opaque")
    assert grant.token_profile == "opaque"
    with pytest.raises(CredentialRefused):
        CredentialUseGrant(req, "dec", "vault:x", 1, 2, "saml")
    with pytest.raises(CredentialRefused) as insecure:
        AuthorizationChallenge(
            req, "https://i", "c", "http://evil.test/cb", "s", "n", "p", 1, 1
        )
    assert insecure.value.refusal is CredentialRefusal.INSECURE_REDIRECT


@pytest.mark.spec("ARBORA-IDENTITY-R023.2")
def test_resolve_scoped_reuse_and_single_refresh() -> None:
    async def scenario() -> None:
        ports = Ports()
        ports.vault["vault:ada:idp"] = {"access": TOKEN}
        assert (await resolve_credential(request(), ports))["access"] == TOKEN
        # An agent with no grant is refused before any token is read.
        denied = Ports(granted=False)
        denied.vault["vault:ada:idp"] = {"access": TOKEN}
        with pytest.raises(CredentialRefused) as caught:
            await resolve_credential(request(), denied)
        assert caught.value.refusal is CredentialRefusal.REUSE_DENIED
        # An expired token refreshes once however many callers wait on the grant.
        stale = Ports()
        stale.vault["vault:ada:idp"] = {"access": "old", "refresh": "r1"}
        records = await asyncio.gather(
            *(resolve_credential(request(), stale) for _ in range(5))
        )
        assert stale.refreshes == 1 and all(r["access"] == TOKEN for r in records)
        # A revoked credential refuses and a missing one with no channel needs interaction.
        revoked = Ports()
        revoked.vault["vault:ada:idp"] = {"access": TOKEN}
        revoked.standing = "revoked"
        with pytest.raises(CredentialRefused) as gone:
            await resolve_credential(request(), revoked)
        assert gone.value.refusal is CredentialRefusal.REVOKED
        quiet = Ports()
        quiet.interactive = False
        with pytest.raises(CredentialRefused) as need:
            await resolve_credential(request(), quiet)
        assert need.value.refusal is CredentialRefusal.INTERACTION_REQUIRED
        assert NOW  # the injected clock is the only time source

    asyncio.run(scenario())
