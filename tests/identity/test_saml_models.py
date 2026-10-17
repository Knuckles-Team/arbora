"""Refusal tests for the .1 typed SAML service-provider model: R012.1."""

import pytest

from arbora.identity.engine import IdentityUnavailable
from arbora.identity.saml import SamlServiceProvider

_CERT = "-----BEGIN CERTIFICATE-----\nMII...\n-----END CERTIFICATE-----"


@pytest.mark.spec("ARBORA-IDENTITY-R012.1")
def test_saml_service_provider_accepts_valid_config() -> None:
    provider = SamlServiceProvider(
        entity_id="urn:arbora:sp",
        acs_url="https://arbora.example.org/saml/acs",
        idp_certificate_pem=_CERT,
    )
    assert provider.entity_id == "urn:arbora:sp"


@pytest.mark.spec("ARBORA-IDENTITY-R012.1")
def test_saml_service_provider_refuses_non_url_acs() -> None:
    with pytest.raises(IdentityUnavailable):
        SamlServiceProvider(
            entity_id="urn:arbora:sp", acs_url="not-a-url", idp_certificate_pem=_CERT
        )


@pytest.mark.spec("ARBORA-IDENTITY-R012.1")
def test_saml_service_provider_refuses_missing_certificate() -> None:
    with pytest.raises(IdentityUnavailable):
        SamlServiceProvider(
            entity_id="urn:arbora:sp",
            acs_url="https://arbora.example.org/saml/acs",
            idp_certificate_pem="not a cert",
        )
