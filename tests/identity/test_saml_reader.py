"""The built-in SAML response reader over real XML signatures (ARBORA-IDENTITY-R012.2.5).

Keys and certificates are generated here for the test only; the ACS runs with no plugin
installed, so the reader is the one Arbora ships.
"""

from __future__ import annotations

import base64
import datetime as dt
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner, methods

from arbora.identity.federated import SAML_ACS
from arbora.identity.saml_reader import SAML, SAMLP, SamlReader
from tests.identity._federated_fixture import (
    NOW,
    SECRETS,
    _world,
    cookie_values,
    post_form,
)
from tests.identity.test_modes_and_browser import ORIGIN

UNIFORM = b'{"error":"Authentication required"}'


def _identity() -> tuple[bytes, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-idp")])
    start = dt.datetime.now(dt.UTC) - timedelta(days=1)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    pem_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem_key, cert.public_bytes(serialization.Encoding.PEM).decode()


KEY, CERT = _identity()
OTHER_KEY, _ = _identity()


def _instant(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _assertion(
    assertion_id: str = "_a1",
    *,
    not_before: dt.datetime = NOW - timedelta(minutes=1),
    not_after: dt.datetime = NOW + timedelta(minutes=5),
    role: str = "staff",
) -> Any:
    return etree.fromstring(
        f"""<saml:Assertion xmlns:saml="{SAML}" ID="{assertion_id}" Version="2.0">
<saml:Issuer>https://idp.invalid</saml:Issuer>
<saml:Subject><saml:NameID>alice-name-id</saml:NameID>
<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
<saml:SubjectConfirmationData Recipient="{ORIGIN + SAML_ACS}"/></saml:SubjectConfirmation>
</saml:Subject>
<saml:Conditions NotBefore="{_instant(not_before)}" NotOnOrAfter="{_instant(not_after)}">
<saml:AudienceRestriction><saml:Audience>urn:arbora:sp</saml:Audience></saml:AudienceRestriction>
</saml:Conditions>
<saml:AttributeStatement><saml:Attribute Name="role">
<saml:AttributeValue>{role}</saml:AttributeValue></saml:Attribute></saml:AttributeStatement>
</saml:Assertion>"""
    )


def _sign(element: Any, key: bytes = KEY) -> Any:
    return XMLSigner(
        method=methods.enveloped,
        signature_algorithm="rsa-sha256",
        digest_algorithm="sha256",
        c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
    ).sign(element, key=key, cert=CERT, reference_uri="#" + element.get("ID"))


def _response(*assertions: Any, response_id: str = "_resp") -> Any:
    root = etree.Element(f"{{{SAMLP}}}Response", nsmap={"samlp": SAMLP}, ID=response_id)
    for assertion in assertions:
        root.append(assertion)
    return root


def _encode(root: Any) -> str:
    return base64.b64encode(etree.tostring(root)).decode("ascii")


@pytest.fixture(name="signed")
def signed_world(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The served auth app with the shipped reader and a generated IdP certificate."""
    from medulla.generated import models

    from arbora.identity.federated import LOGIN_STATE_MODELS

    for name in (*LOGIN_STATE_MODELS, "IdentityOpTokenRegisterAssertion"):
        monkeypatch.setattr(models, name, object, raising=False)
    monkeypatch.setitem(SECRETS, "env://SAML_CERT", CERT)
    return _world(monkeypatch, reader=SamlReader())


def _acs(world: SimpleNamespace, root: Any) -> Any:
    return post_form(world, SAML_ACS, {"SAMLResponse": _encode(root)})


@pytest.mark.pin_lag
@pytest.mark.spec("ARBORA-IDENTITY-R012.2.5")
def test_valid_signed_assertion_signs_in_with_no_plugin_installed(
    signed: SimpleNamespace,
) -> None:
    status, headers, _ = _acs(signed, _response(_sign(_assertion())))
    assert status == 303 and cookie_values(headers)
    login = signed.engine.logins[0]
    assert login["subject"] == "alice-name-id" and login["claims"] == {
        "role": ["staff"]
    }
    # A response signed as a whole, carrying the one assertion, is read the same way.
    signed_response = _sign(_response(_assertion("_a2"), response_id="_r2"))
    assert _acs(signed, signed_response)[0] == 303
    # The entry point of the group resolves to the shipped reader.
    from arbora.identity.federated import _entry_point_reader

    assert isinstance(_entry_point_reader(), SamlReader)


@pytest.mark.pin_lag
@pytest.mark.spec("ARBORA-IDENTITY-R012.2.5")
def test_wrong_signature_is_refused(signed: SimpleNamespace) -> None:
    status, headers, body = _acs(signed, _response(_sign(_assertion(), OTHER_KEY)))
    assert (status, body) == (401, UNIFORM) and not cookie_values(headers)
    assert signed.audit[-1]["outcome"] == "SAML_SIGNATURE_INVALID"
    # A tampered signed assertion no longer matches its digest.
    tampered = _sign(_assertion("_t1"))
    tampered.find(
        f"{{{SAML}}}AttributeStatement//{{{SAML}}}AttributeValue"
    ).text = "root"
    assert _acs(signed, _response(tampered))[0] == 401
    assert signed.audit[-1]["outcome"] == "SAML_SIGNATURE_INVALID"
    assert not signed.engine.logins


@pytest.mark.pin_lag
@pytest.mark.spec("ARBORA-IDENTITY-R012.2.5")
def test_unsigned_assertion_is_refused(signed: SimpleNamespace) -> None:
    status, headers, body = _acs(signed, _response(_assertion()))
    assert (status, body) == (401, UNIFORM) and not cookie_values(headers)
    assert signed.audit[-1]["outcome"] == "SAML_UNSIGNED"
    assert not signed.engine.logins


@pytest.mark.pin_lag
@pytest.mark.spec("ARBORA-IDENTITY-R012.2.5")
def test_expired_and_not_yet_valid_assertions_are_refused(
    signed: SimpleNamespace,
) -> None:
    expired = _assertion(
        "_e1",
        not_before=NOW - timedelta(minutes=9),
        not_after=NOW - timedelta(minutes=1),
    )
    assert _acs(signed, _response(_sign(expired)))[0] == 401
    assert signed.audit[-1]["outcome"] == "SAML_EXPIRED"
    early = _assertion(
        "_e2",
        not_before=NOW + timedelta(minutes=1),
        not_after=NOW + timedelta(minutes=9),
    )
    assert _acs(signed, _response(_sign(early)))[0] == 401
    assert signed.audit[-1]["outcome"] == "SAML_EXPIRED"
    assert not signed.engine.logins


@pytest.mark.pin_lag
@pytest.mark.spec("ARBORA-IDENTITY-R012.2.5")
def test_wrapped_assertion_is_refused(signed: SimpleNamespace) -> None:
    # A second, forged assertion beside the signed one is a signature-wrapping attempt.
    forged = _assertion("_forged", role="admin")
    status, _, body = _acs(signed, _response(forged, _sign(_assertion("_real"))))
    assert (status, body) == (401, UNIFORM)
    assert signed.audit[-1]["outcome"] == "SAML_SIGNATURE_WRAPPING"
    assert not signed.engine.logins


@pytest.mark.spec("ARBORA-IDENTITY-R012.2.5")
def test_reader_refuses_a_doctype_and_garbage() -> None:
    reader = SamlReader()
    for text in ("!!", base64.b64encode(b"<!DOCTYPE x><a/>").decode()):
        with pytest.raises(ValueError):
            reader.read(text)
