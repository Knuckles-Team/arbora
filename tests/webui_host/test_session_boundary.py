"""Composition fixtures, not deployed login or signed credential qualification."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from arbora.identity.engine import IdentityUnavailable
from arbora.webui_host.session_boundary import (
    BrowserSessionMiddleware,
    identity_factory_kwargs,
)


@pytest.mark.spec("ARBORA-IDENTITY-R004.7.1")
def test_explicit_mode_composition_never_falls_back():
    for mode in ("required", "none", "oidc"):
        assert identity_factory_kwargs(mode, None) == (
            {} if mode == "required" else {"auth_mode": mode}
        )
    for mode in ("eg", "", "unknown"):
        with pytest.raises(IdentityUnavailable):
            identity_factory_kwargs(mode, None)
    with pytest.raises(IdentityUnavailable):
        identity_factory_kwargs("none", lambda app: None)


@pytest.mark.spec("ARBORA-IDENTITY-R004.7.1")
def test_webui_mode_is_typed_and_protected_by_default():
    from arbora.core.settings import ArboraConfig

    assert ArboraConfig().webui_auth_mode == "required"
    assert ArboraConfig(WEBUI_AUTH_MODE="none").webui_auth_mode == "none"
    with pytest.raises(ValueError):
        ArboraConfig(WEBUI_AUTH_MODE="invalid")


@pytest.mark.parametrize(
    "error,status",
    [
        (PermissionError("revoked"), 401),
        (IdentityUnavailable("private-backend-detail"), 503),
    ],
)
@pytest.mark.spec("ARBORA-IDENTITY-R004.4.1")
def test_admission_failure_does_not_forward_or_reflect_details(error, status):
    @asynccontextmanager
    async def refuse(scope):
        raise error
        yield scope

    calls = []

    async def app(scope, receive, send):
        calls.append(scope)

    middleware = BrowserSessionMiddleware(
        app, admission=SimpleNamespace(request=refuse), auth_app=app
    )
    scope = {
        "type": "http",
        "path": "/api/run",
        "headers": [
            (
                b"cookie",
                b"__Host-arbora_session="
                + b"c3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3M",
            )
        ],
    }
    messages = []

    async def send(message):
        messages.append(message)

    asyncio.run(middleware(scope, None, send))
    assert calls == []
    assert messages[0]["status"] == status
    assert str(error).encode() not in messages[1]["body"]


@pytest.mark.spec("ARBORA-IDENTITY-R004.4.1")
def test_exact_auth_namespace_owned_and_bearer_requests_stay_with_identity_gate():
    calls = []

    async def app(scope, receive, send):
        calls.append("protected")

    async def auth(scope, receive, send):
        calls.append("auth")

    middleware = BrowserSessionMiddleware(app, admission=None, auth_app=auth)

    async def scenario():
        for path in ("/auth", "/auth/login", "/authentication", "/api/tasks"):
            await middleware({"type": "http", "path": path, "headers": []}, None, None)

    asyncio.run(scenario())
    assert calls == ["auth", "auth", "protected", "protected"]


@pytest.mark.spec("ARBORA-IDENTITY-R004.4.1")
def test_admitted_scope_lifetime_and_failure_cleanup():
    events = []

    @asynccontextmanager
    async def admit(scope):
        events.append("admit")
        try:
            yield {**scope, "fixture_marker": True}
        finally:
            events.append("cleanup")

    async def app(scope, receive, send):
        assert scope["fixture_marker"] is True
        events.append("invoke")
        raise RuntimeError("fixture downstream failure")

    middleware = BrowserSessionMiddleware(
        app, admission=SimpleNamespace(request=admit), auth_app=app
    )
    scope = {
        "type": "http",
        "path": "/api/run",
        "headers": [
            (
                b"cookie",
                b"__Host-arbora_session="
                + b"c3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3M",
            )
        ],
    }
    with pytest.raises(RuntimeError, match="fixture downstream"):
        asyncio.run(middleware(scope, None, None))
    assert events == ["admit", "invoke", "cleanup"]
    assert "fixture_marker" not in scope


@pytest.mark.spec("ARBORA-IDENTITY-R004.4.1")
def test_incomplete_shared_caller_contract_cannot_activate_boundary():
    from arbora.webui_host.session_boundary import WebUISessionBoundary

    # No credentials or issuer are constructed. The activation gate must refuse
    # even a composition-owned object until shared dispatch is qualified.
    boundary = object.__new__(WebUISessionBoundary)
    with pytest.raises(IdentityUnavailable, match="invocation fence"):
        boundary(None)


@pytest.mark.spec("ARBORA-IDENTITY-R004.7.1")
def test_old_installed_factory_refuses_explicit_modes_without_downgrade():
    def old_factory(agent, *, session_boundary=None):
        pass

    assert identity_factory_kwargs("required", None, factory=old_factory) == {}
    with pytest.raises(IdentityUnavailable, match="lacks explicit"):
        identity_factory_kwargs("none", None, factory=old_factory)
