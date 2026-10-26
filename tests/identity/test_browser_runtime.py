"""Synthetic source acceptance, never deployed login or native grant proof."""

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from arbora.identity.browser import SESSION_COOKIE, csrf_token_for
from arbora.identity.browser_runtime import (
    BrowserAuthApplication,
    BrowserCallerResolver,
)
from arbora.identity.engine import IdentityUnavailable, Resolution
from arbora.identity.issuer import claims_for
from arbora.identity.ports import SessionState

from .test_engine_session_authority import reply_value
from .test_issuer import SETTINGS
from .test_issuer import synthetic_signing_key as synthetic_signing_key
from .test_modes_and_browser import ORIGIN, TOKEN

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.4.1")


class Authority:
    def __init__(self):
        self.resolution = Resolution.from_reply(reply_value())
        self.state = SessionState(
            self.resolution, int(time.time() + 120) * 1000, "fixture-reference", None
        )
        self.calls = []
        self.error = None

    async def resolve_session(self, credential):
        self.calls.append(credential)
        if self.error:
            raise self.error
        return self.state

    async def verify(self, token):
        assert token == "fixture-jwt"
        return SimpleNamespace(
            claims=claims_for(
                self.resolution,
                SETTINGS,
                source_expires_at_ms=self.state.expires_at_ms,
                now_ms=int(time.time() * 1000),
            )
        )


def test_converter_preserves_exact_engine_authority():
    async def scenario():
        authority = Authority()
        caller = await BrowserCallerResolver(
            authority, authority.verify, graph="fixture-graph"
        )("fixture-jwt", TOKEN)
        assert authority.calls == [TOKEN]
        assert caller.actor.roles == ("reader",)
        assert caller.scopes == frozenset({"kg:read"})
        assert caller.tenant == "fixture-tenant"
        assert caller.policy_version == "fixture-policy"
        assert caller.graph == "fixture-graph"
        assert (
            caller.actor.credential_expires_at * 1000 <= authority.state.expires_at_ms
        )
        expected = dict(authority.resolution.request_context)
        for name in ("roles", "scopes", "delegation"):
            expected[name] = list(expected[name])
        assert caller.engine_verified_context() == expected

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault", ["revoke", "policy", "tenant", "scope", "expiry", "service", "missing"]
)
def test_converter_refuses_changed_or_missing_authority(fault):
    async def scenario():
        authority = Authority()

        async def verify(token):
            verified = await authority.verify(token)
            if fault == "revoke":
                authority.error = PermissionError("revoked")
            elif fault == "missing":
                authority.state = None
            elif fault == "service":
                authority.state = replace(
                    authority.state,
                    resolution=replace(authority.resolution, kind="service"),
                )
            else:
                field = {
                    "policy": "policy_version",
                    "tenant": "tenant_id",
                    "scope": "scope",
                    "expiry": "exp",
                }[fault]
                verified.claims[field] = 4102444800 if fault == "expiry" else "changed"
            return verified

        with pytest.raises((PermissionError, IdentityUnavailable)):
            await BrowserCallerResolver(authority, verify, graph="fixture-graph")(
                "fixture-jwt", TOKEN
            )

    asyncio.run(scenario())


def auth_scope():
    return {
        "type": "http",
        "method": "POST",
        "path": "/auth/login",
        "scheme": "https",
        "headers": [
            (b"host", b"console.invalid"),
            (b"origin", ORIGIN.encode()),
            (b"content-type", b"application/json"),
        ],
    }


async def request(app, scope, body):
    messages = []

    async def receive():
        return {"type": "http.request", "body": body}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return messages[0], b"".join(m.get("body", b"") for m in messages[1:])


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "denied",
        "mfa",
        "missing-key",
        "revoke",
        "origin",
        "csrf",
        "oversized",
        "wrong-host",
        "http",
        "logout",
    ],
)
def test_auth_routes_refuse_and_never_return_tokens(monkeypatch, fault):
    async def scenario():
        calls = []
        authority = Authority()

        class Issuer:
            async def issue(self, credential):
                if fault == "missing-key":
                    raise IdentityUnavailable("unconfigured key")
                return "fixture-jwt"

        async def identity_value(client, graph, operation, kind):
            calls.append(operation)
            if operation["op"] == "revoke":
                return {"changed": True}
            assert operation["request"]["username"] == "fixture"
            if fault == "revoke":
                authority.error = PermissionError("revoked")
            return {
                "outcome": "bad"
                if fault == "denied"
                else "mfa_required"
                if fault == "mfa"
                else "ok"
            }

        monkeypatch.setattr(
            "arbora.identity.approval_engine.identity_value", identity_value
        )
        app = BrowserAuthApplication(
            object(),
            authority,
            graph="fixture-graph",
            origin=ORIGIN,
            issuer=Issuer(),
            caller=BrowserCallerResolver(
                authority, authority.verify, graph="fixture-graph"
            ),
        )
        scope = auth_scope()
        body = b'{"username":"fixture","password":"fixture-password"}'
        if fault == "origin":
            scope["headers"][1] = (b"origin", b"https://other.invalid")
        if fault == "wrong-host":
            scope["headers"][0] = (b"host", b"other.invalid")
        if fault == "http":
            scope["scheme"] = "http"
        if fault == "oversized":
            body = b"x" * 4097
        if fault in {"logout", "csrf"}:
            scope["path"] = "/auth/logout"
            scope["headers"].append((b"cookie", f"{SESSION_COOKIE}={TOKEN}".encode()))
            if fault == "logout":
                scope["headers"].append(
                    (b"x-csrf-token", csrf_token_for(TOKEN).encode())
                )
        start, response = await request(app, scope, body)
        headers = dict(start["headers"])
        assert b"fixture-jwt" not in response and b"fixture-password" not in response
        assert headers[b"cache-control"] == b"no-store"
        if fault is None:
            assert start["status"] == 200
            for flag in [b"Secure", b"HttpOnly", b"SameSite=Lax", b"Path=/"]:
                assert flag in headers[b"set-cookie"]
            assert "csrf_token" in json.loads(response)
            assert len(calls) == 1
        elif fault == "logout":
            assert start["status"] == 204 and b"Max-Age=0" in headers[b"set-cookie"]
            assert [op["op"] for op in calls] == ["revoke"]
        else:
            assert start["status"] == (503 if fault == "missing-key" else 401)
            assert b"set-cookie" not in headers
            if fault in {"origin", "csrf", "oversized", "wrong-host", "http"}:
                assert calls == []
            else:
                assert [op["op"] for op in calls] == ["authenticate", "revoke"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault", [None, "revoked", "origin", "no-cookie", "missing-key"]
)
def test_session_refresh_is_current_and_never_exposes_bearer(fault):
    async def scenario():
        authority = Authority()

        class Issuer:
            async def issue(self, credential):
                assert credential == TOKEN
                if fault == "missing-key":
                    raise IdentityUnavailable("no key")
                return "fixture-jwt"

        if fault == "revoked":
            authority.error = PermissionError("revoked")
        app = BrowserAuthApplication(
            object(),
            authority,
            graph="fixture-graph",
            origin=ORIGIN,
            issuer=Issuer(),
            caller=BrowserCallerResolver(
                authority, authority.verify, graph="fixture-graph"
            ),
        )
        scope = auth_scope()
        scope.update(method="GET", path="/auth/session")
        if fault != "no-cookie":
            scope["headers"].append((b"cookie", f"{SESSION_COOKIE}={TOKEN}".encode()))
        if fault == "origin":
            scope["headers"][1] = (b"origin", b"https://other.invalid")
        start, body = await request(app, scope, b"")
        if fault == "no-cookie":
            assert start["status"] == 200
            assert json.loads(body) == {
                "authenticated": False,
                "mode": "local",
                "roles": [],
                "webui_role": None,
            }
        elif fault:
            assert start["status"] == (503 if fault == "missing-key" else 401)
            if fault == "origin":
                assert b"set-cookie" not in dict(start["headers"])
            elif fault == "revoked":
                assert b"Max-Age=0" in dict(start["headers"])[b"set-cookie"]
        else:
            assert start["status"] == 200
            assert json.loads(body) == {
                "authenticated": True,
                "mode": "local",
                "csrf_token": csrf_token_for(TOKEN),
                "subject": "usr:fixture",
                "tenant": "fixture-tenant",
                "roles": ["reader"],
                "webui_role": "reader",
            }
        assert b"fixture-jwt" not in body and TOKEN.encode() not in body

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        b'{"username":"a","username":"b","password":"c"}',
        b"[]",
        b'{"username":"a","password":5}',
        b'{"username":"a","password":"b","new_password":"c"}',
    ],
)
def test_malformed_login_does_not_reach_owner(monkeypatch, body):
    async def owner(*args):
        pytest.fail("malformed login reached native owner")

    monkeypatch.setattr("arbora.identity.approval_engine.identity_value", owner)
    app = BrowserAuthApplication(
        object(),
        object(),
        graph="fixture-graph",
        origin=ORIGIN,
        issuer=object(),
        caller=object(),
    )
    start, _ = asyncio.run(request(app, auth_scope(), body))
    assert start["status"] == 401


@pytest.mark.parametrize("failure", ["send", "cancel"])
def test_failed_response_delivery_revokes_fresh_session_and_releases_capacity(failure):
    from starlette.responses import JSONResponse

    async def scenario():
        app = BrowserAuthApplication(
            object(),
            object(),
            graph="fixture-graph",
            origin=ORIGIN,
            issuer=object(),
            caller=object(),
        )
        revoked = []

        async def response(scope, receive):
            value = JSONResponse({"csrf_token": "fixture-csrf"})
            value._created_session = TOKEN
            return value

        async def revoke(credential):
            await asyncio.sleep(0)
            revoked.append(credential)

        async def send(message):
            if failure == "cancel":
                raise asyncio.CancelledError
            raise ConnectionError("disconnected")

        app._response, app._revoke = response, revoke
        with pytest.raises((ConnectionError, asyncio.CancelledError)):
            await app(auth_scope(), object(), send)
        assert revoked == [TOKEN] and app._in_flight == 0

    asyncio.run(scenario())


def test_generated_contract_login_session_logout_with_ephemeral_signing_key(
    synthetic_signing_key,
):
    """Real decoder/signature/owners; synthetic wire, NOT a native EG deployment."""
    from joserfc import jwt
    from joserfc.jwk import RSAKey

    from arbora.identity.engine_ports import EngineSessionAuthority
    from arbora.identity.issuer import LocalIssuer
    from arbora.security.request_identity import VerifiedLocalBearer

    from .test_issuer import ExistingFixtureKeyStore

    async def scenario():
        live = set()
        operations = []

        class Wire:
            async def _send(self, method, params, graph, **kwargs):
                assert method == "Identity" and graph == "fixture-graph"
                op = params["op"]
                operations.append(op["op"])
                credential = op["request"]["session_token"]
                if op["op"] == "authenticate":
                    live.add(credential)
                    return {"kind": "authenticate", "value": {"outcome": "ok"}}
                if op["op"] == "revoke":
                    live.discard(credential)
                    return {"kind": "done", "value": {"changed": True}}
                assert op["request"]["refresh"] is False
                if credential not in live:
                    raise PermissionError("revoked fixture")
                return reply_value()

        wire = Wire()
        authority = EngineSessionAuthority(wire, graph="fixture-graph", refresh=False)
        issuer = LocalIssuer(
            ExistingFixtureKeyStore(synthetic_signing_key), SETTINGS, authority
        )

        async def verify(token):
            decoded = jwt.decode(
                token, RSAKey.import_key(synthetic_signing_key), algorithms=["RS256"]
            )
            jwt.JWTClaimsRegistry(
                iss={"essential": True, "value": SETTINGS.issuer},
                aud={"essential": True, "value": SETTINGS.audience},
                exp={"essential": True},
            ).validate(decoded.claims)
            return VerifiedLocalBearer(
                decoded.claims, SETTINGS.issuer, SETTINGS.audience
            )

        app = BrowserAuthApplication(
            wire,
            authority,
            graph="fixture-graph",
            origin=ORIGIN,
            issuer=issuer,
            caller=BrowserCallerResolver(authority, verify, graph="fixture-graph"),
        )
        start, body = await request(
            app, auth_scope(), b'{"username":"fixture","password":"fixture-password"}'
        )
        assert start["status"] == 200
        cookie = dict(start["headers"])[b"set-cookie"].split(b";", 1)[0]
        csrf = json.loads(body)["csrf_token"]
        assert len(live) == 1 and next(iter(live)).encode() not in body
        scope = auth_scope()
        scope.update(method="GET", path="/auth/session")
        scope["headers"].append((b"cookie", cookie))
        assert (await request(app, scope, b""))[0]["status"] == 200
        assert b"set-cookie" not in dict((await request(app, scope, b""))[0]["headers"])
        scope.update(method="POST", path="/auth/logout")
        scope["headers"].append((b"x-csrf-token", csrf.encode()))
        assert (await request(app, scope, b""))[0]["status"] == 204
        assert not live
        scope.update(method="GET", path="/auth/session")
        assert (await request(app, scope, b""))[0]["status"] == 401
        assert operations.count("authenticate") == operations.count("revoke") == 1

    asyncio.run(scenario())
