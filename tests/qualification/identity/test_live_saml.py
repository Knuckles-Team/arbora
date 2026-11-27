"""Live SAML sign-in against the deployed identity provider (ARBORA-IDENTITY-R012.2.4, L6).

Variables: ``ARBORA_QUAL_SAML_BASE_URL`` (the host), ``ARBORA_QUAL_SAML_ASSERTION`` (a
base64 ``SAMLResponse`` the provider issued for the test user to this host's ACS URL) and
``ARBORA_QUAL_SAML_PRINCIPAL_DIGEST`` (the first 16 hex characters of the SHA-256 of the
expected principal).
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse

import pytest
from _identity_live import request, require


@pytest.mark.spec("ARBORA-IDENTITY-R012.2.4")
def test_live_saml_sign_in() -> None:
    env = require(
        "ARBORA_QUAL_SAML_BASE_URL",
        "ARBORA_QUAL_SAML_ASSERTION",
        "ARBORA_QUAL_SAML_PRINCIPAL_DIGEST",
    )
    base = env["ARBORA_QUAL_SAML_BASE_URL"].rstrip("/")
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(base))
    status, headers, _ = request(
        base,
        "POST",
        "/auth/saml/acs",
        {"SAMLResponse": env["ARBORA_QUAL_SAML_ASSERTION"]},
        {"Origin": origin},
    )
    cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
    assert status == 200 and cookie
    status, _, body = request(base, "GET", "/auth/session", None, {"Cookie": cookie})
    assert status == 200
    principal = str(json.loads(body).get("principal", ""))
    digest = hashlib.sha256(principal.encode()).hexdigest()[:16]
    assert digest == env["ARBORA_QUAL_SAML_PRINCIPAL_DIGEST"]
    # The same assertion is replayed once only.
    again, _, _ = request(
        base,
        "POST",
        "/auth/saml/acs",
        {"SAMLResponse": env["ARBORA_QUAL_SAML_ASSERTION"]},
        {"Origin": origin},
    )
    assert again == 401
