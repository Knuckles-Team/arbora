"""Disposable, verified loopback issuer for certification tests."""

import pytest

from arbora.certification.oidc import EphemeralLoopbackOidcAuthority


@pytest.fixture
def certification_authority():
    with EphemeralLoopbackOidcAuthority() as authority:
        yield authority
