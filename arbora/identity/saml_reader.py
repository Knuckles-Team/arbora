"""The SAML response reader: parse, then verify the XML signature (ARBORA-IDENTITY-R012.2.5).

The XML-DSig work is done by ``signxml``, a maintained library; no signature code is written
here. ``verify`` accepts an assertion only when the signed element, re-read from what the
library verified, equals the assertion that ``read`` returned, so a wrapped or substituted
assertion fails. Unsigned, wrongly signed and wrapped responses are refused; the conditions
(audience, recipient, window) are read here and checked by ``arbora.identity.saml``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lxml import etree

from .engine import IdentityUnavailable
from .federated import ReadSamlResponse
from .saml import ParsedSamlAssertion

SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
DS = "http://www.w3.org/2000/09/xmldsig#"
RESPONSE_LIMIT = 1_048_576


@dataclass(frozen=True, slots=True)
class SignedAssertion(ParsedSamlAssertion):
    """A read assertion that carries the bytes and attributes the verifier re-reads."""

    attributes: tuple[tuple[str, tuple[str, ...]], ...] = ()
    source: bytes = field(default=b"", compare=False, repr=False)


def _parser() -> Any:
    return etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False
    )


def _text(element: Any, path: str) -> str:
    found = element.find(path, {"s": SAML})
    if found is None or not (found.text or "").strip():
        raise ValueError(f"SAML assertion lacks {path}")
    return found.text.strip()


def _instant(value: str | None) -> datetime:
    if not value:
        raise ValueError("SAML condition lacks its time")
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("SAML time lacks a zone")
    return moment.astimezone(UTC)


def _attributes(assertion: Any) -> tuple[tuple[str, tuple[str, ...]], ...]:
    found: dict[str, list[str]] = {}
    for attribute in assertion.iterfind(
        "s:AttributeStatement/s:Attribute", {"s": SAML}
    ):
        values = [
            (v.text or "").strip()
            for v in attribute.iterfind("s:AttributeValue", {"s": SAML})
        ]
        found.setdefault(attribute.get("Name", ""), []).extend(values)
    return tuple(sorted((name, tuple(values)) for name, values in found.items()))


def _assertion_of(element: Any, source: bytes) -> SignedAssertion:
    """The assertion facts of one ``Assertion`` element; ``ValueError`` when malformed."""
    conditions = element.find("s:Conditions", {"s": SAML})
    data = element.find(
        "s:Subject/s:SubjectConfirmation/s:SubjectConfirmationData", {"s": SAML}
    )
    if conditions is None or data is None:
        raise ValueError("SAML assertion lacks its conditions")
    audiences = tuple(
        (a.text or "").strip()
        for a in conditions.iterfind("s:AudienceRestriction/s:Audience", {"s": SAML})
    )
    try:
        return SignedAssertion(
            element.get("ID", ""),
            _text(element, "s:Issuer"),
            _text(element, "s:Subject/s:NameID"),
            audiences,
            data.get("Recipient", ""),
            _instant(conditions.get("NotBefore")),
            _instant(conditions.get("NotOnOrAfter")),
            _attributes(element),
            source,
        )
    except IdentityUnavailable as malformed:
        raise ValueError(str(malformed)) from None


def _signs_assertion_or_response(signature: Any, assertion: Any, root: Any) -> bool:
    """The signature sits in the assertion or the response and references that element."""
    parent = signature.getparent()
    if parent is not assertion and parent is not root:
        return False
    reference = signature.find(f"{{{DS}}}SignedInfo/{{{DS}}}Reference")
    return reference is not None and reference.get("URI") == "#" + parent.get("ID", "")


def _config() -> Any:
    from signxml import SignatureConfiguration, SignatureMethod

    asymmetric = frozenset(
        method for method in SignatureMethod if not method.name.startswith("HMAC")
    )
    return SignatureConfiguration(require_x509=True, signature_methods=asymmetric)


class SamlReader:
    """The ``SamlResponseReader`` over ``signxml`` (entry point ``arbora.identity.saml_reader``)."""

    def read(self, response: str) -> ReadSamlResponse:
        """Parse a base64 response; ``ValueError`` when it is not a SAML response."""
        raw = base64.b64decode(response.encode("ascii"), validate=True)
        if not raw or len(raw) > RESPONSE_LIMIT or b"<!DOCTYPE" in raw.upper():
            raise ValueError("not a SAML response")
        root = etree.fromstring(raw, _parser())
        assertions = list(root.iter(f"{{{SAML}}}Assertion"))
        if not assertions:
            raise ValueError("no SAML assertion")
        read = _assertion_of(assertions[0], raw)
        signatures = list(root.iter(f"{{{DS}}}Signature"))
        signed = (
            len(assertions) == 1
            and len(signatures) == 1
            and _signs_assertion_or_response(signatures[0], assertions[0], root)
        )
        return ReadSamlResponse(read, dict(read.attributes), len(signatures), signed)

    def verify(self, assertion: ParsedSamlAssertion, idp_certificate_pem: str) -> bool:
        """True only when the IdP certificate signed exactly this assertion."""
        if not isinstance(assertion, SignedAssertion):
            return False
        from signxml import XMLVerifier

        try:
            result = XMLVerifier().verify(
                assertion.source,
                x509_cert=idp_certificate_pem,
                expect_config=_config(),
                id_attribute="ID",
            )
            signed = result.signed_xml
            tag = etree.QName(signed)
            found = (
                [signed]
                if tag.text == f"{{{SAML}}}Assertion"
                else list(signed.iter(f"{{{SAML}}}Assertion"))
            )
            return len(found) == 1 and _assertion_of(found[0], b"") == assertion
        except Exception:  # noqa: BLE001 - any failure to verify is an unverified assertion
            return False
