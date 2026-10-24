"""A configured local browser transport never upgrades anonymous authority."""

import pytest

from arbora.security import anonymous
from tests.security.test_anonymous_mode import local_mode as local_mode

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.6")


def _scope(**changes):
    return {
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "client": ("127.0.0.1", 1000),
        "headers": [
            (b"host", b"127.0.0.1:38001"),
            (b"origin", b"http://127.0.0.1:38001"),
        ],
        **changes,
    }


def _transport():
    from arbora.identity.local_browser import LocalBrowserTransport

    return LocalBrowserTransport(origin="http://127.0.0.1:38001")


def test_configured_transport_preserves_anonymous_session(local_mode):
    transport = _transport()
    session = anonymous.session()
    assert transport.validate(_scope(), session) is None
    assert session.actor.authenticated is False
    with pytest.raises(PermissionError, match="Origin"):
        anonymous.validate_request(_scope())


def test_transport_requires_configured_anonymous_mode():
    with pytest.raises(PermissionError):
        _transport()


@pytest.mark.parametrize("kind", ["http", "websocket"])
def test_transport_checks_both_request_kinds(local_mode, kind):
    scope = _scope(type=kind, scheme="ws" if kind == "websocket" else "http")
    assert _transport().validate(scope, anonymous.session()) is None


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"host", b"127.0.0.1:38002"), (b"origin", b"http://127.0.0.1:38001")],
        [(b"host", b"127.0.0.1:38001"), (b"origin", b"https://evil.example")],
        [(b"host", b"127.0.0.1:38001"), (b"origin", b"null")],
    ],
)
def test_missing_or_changed_origin_and_host_refuse(local_mode, headers):
    with pytest.raises(PermissionError):
        _transport().validate(_scope(headers=headers), anonymous.session())


@pytest.mark.parametrize(
    "name",
    [b"host", b"origin", b"authorization", b"cookie", b"forwarded", b"x-forwarded-for"],
)
def test_duplicate_authority_credentials_and_proxy_headers_refuse(local_mode, name):
    scope = _scope()
    scope["headers"].append((name, b"untrusted"))
    with pytest.raises(PermissionError):
        _transport().validate(scope, anonymous.session())


@pytest.mark.parametrize("client", [None, (), ("192.0.2.1", 1000)])
def test_nonlocal_peer_refuses(local_mode, client):
    with pytest.raises(PermissionError):
        _transport().validate(_scope(client=client), anonymous.session())


def test_authority_is_checked_again_for_each_request(local_mode, monkeypatch):
    transport = _transport()
    session = anonymous.session()
    transport.validate(_scope(), session)
    monkeypatch.setattr(anonymous, "_ACTIVE_MODE", "required")
    with pytest.raises(PermissionError):
        transport.validate(_scope(), session)


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:38001",
        "https://example.com:443",
        "http://127.0.0.1",
        "http://127.0.0.1:38001/",
        "http://user@127.0.0.1:38001",
        "http://127.0.0.1:38001?query=1",
        "http://127.0.0.1:38001#fragment",
        "http://127.0.0.1:999999",
    ],
)
def test_invalid_operator_origin_configuration_refuses(local_mode, origin):
    from arbora.identity.local_browser import LocalBrowserTransport

    with pytest.raises(ValueError):
        LocalBrowserTransport(origin=origin)


@pytest.mark.parametrize("scheme", ["https", "ws", "file"])
def test_http_scheme_confusion_refuses(local_mode, scheme):
    with pytest.raises(PermissionError):
        _transport().validate(_scope(scheme=scheme), anonymous.session())
