"""The optional public browser port has no public authority issuer."""

import asyncio

import pytest

from arbora.client import browser_boundary

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.6")


def test_public_boundary_constructor_refuses():
    with pytest.raises(TypeError, match="must be issued by its owner"):
        browser_boundary.LocalBrowserBoundary()


def test_public_boundary_contract_has_no_issuer():
    assert not any(
        name.startswith(("issue", "create", "grant")) for name in vars(browser_boundary)
    )
    assert browser_boundary.LOCAL_BROWSER_PATH == "/ws/local-browser-admission"


def test_uninitialized_nominal_instance_cannot_dispatch():
    boundary = object.__new__(browser_boundary.LocalBrowserBoundary)
    with pytest.raises(AttributeError, match="_handler"):
        asyncio.run(boundary({}, None, None))


def test_public_package_reexports_the_same_nominal_contract():
    from arbora.client import LOCAL_BROWSER_PATH, LocalBrowserBoundary

    assert LocalBrowserBoundary is browser_boundary.LocalBrowserBoundary
    assert LOCAL_BROWSER_PATH == browser_boundary.LOCAL_BROWSER_PATH
    with pytest.raises(TypeError, match="must be issued by its owner"):
        LocalBrowserBoundary()
