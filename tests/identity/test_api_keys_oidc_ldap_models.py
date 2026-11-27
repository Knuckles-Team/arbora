"""Refusal tests for the .1 typed models: R008, R010."""

import pytest

from arbora.identity.api_keys import ApiKeyGrant
from arbora.identity.engine import IdentityUnavailable
from arbora.identity.ldap import LdapBindConfig


def test_api_key_accepts_non_approver_scopes() -> None:
    grant = ApiKeyGrant(
        key_id="k1", owner_principal_id="p1", scopes=frozenset({"domain:read"})
    )
    assert "domain:read" in grant.scopes


def test_api_key_refuses_approver_scope() -> None:
    with pytest.raises(IdentityUnavailable):
        ApiKeyGrant(
            key_id="k1", owner_principal_id="p1", scopes=frozenset({"approver"})
        )


def test_api_key_refuses_empty_owner() -> None:
    with pytest.raises(IdentityUnavailable):
        ApiKeyGrant(key_id="k1", owner_principal_id="", scopes=frozenset())


def test_ldap_bind_accepts_ldaps() -> None:
    config = LdapBindConfig(
        host="dc.example.org", port=636, bind_dn="cn=svc,dc=example,dc=org"
    )
    assert config.scheme == "ldaps"


def test_ldap_bind_refuses_plaintext_scheme() -> None:
    with pytest.raises(IdentityUnavailable):
        LdapBindConfig(
            host="dc.example.org",
            port=389,
            bind_dn="cn=svc,dc=example,dc=org",
            scheme="ldap",
        )


def test_ldap_bind_refuses_invalid_port() -> None:
    with pytest.raises(IdentityUnavailable):
        LdapBindConfig(
            host="dc.example.org", port=0, bind_dn="cn=svc,dc=example,dc=org"
        )
