"""Exit obligation ``login_probe``: a browser login reaches a verified session (ARBORA-RELEASE-R003.7).

The pull-request run drives the browser login through the loopback issuer with
an ephemeral signing key. The live-issuer run of ``scripts/certification_oidc.py``
is a release-stage receipt and does not gate the matrix.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace  # noqa: F401

import pytest

from tests.identity.test_browser_runtime import auth_scope, request
from tests.identity.test_engine_session_authority import reply_value
from tests.identity.test_issuer import SETTINGS, ExistingFixtureKeyStore
from tests.identity.test_issuer import (
    synthetic_signing_key as synthetic_signing_key,  # noqa: F401
)
from tests.identity.test_modes_and_browser import ORIGIN


class Wire:
    """The engine identity wire: authenticate mints, resolve reads, revoke ends."""

    def __init__(self) -> None:
        self.live: set[str] = set()
        self.operations: list[str] = []

    async def _send(self, method, params, graph, **kwargs):
        from medulla.client import EngineResponseError

        assert method == "Identity" and graph == "fixture-graph"
        op = params["op"]
        self.operations.append(op["op"])
        credential = op["request"]["session_token"]
        if op["op"] == "authenticate":
            self.live.add(credential)
            return {"kind": "authenticate", "value": {"outcome": "ok"}}
        if op["op"] == "revoke":
            self.live.discard(credential)
            return {"kind": "done", "value": {"changed": True}}
        if credential not in self.live:
            raise EngineResponseError("IDENTITY_NOT_FOUND", "revoked fixture")
        return reply_value()


def _application(wire: Wire, key: object) -> object:
    from joserfc import jwt
    from joserfc.jwk import RSAKey

    from arbora.identity.browser_runtime import (
        BrowserAuthApplication,
        BrowserCallerResolver,
    )
    from arbora.identity.engine_ports import EngineSessionAuthority
    from arbora.identity.issuer import LocalIssuer
    from arbora.security.request_identity import VerifiedLocalBearer

    authority = EngineSessionAuthority(wire, graph="fixture-graph", refresh=False)
    issuer = LocalIssuer(ExistingFixtureKeyStore(key), SETTINGS, authority)

    async def verify(token: str) -> VerifiedLocalBearer:
        decoded = jwt.decode(token, RSAKey.import_key(key), algorithms=["RS256"])
        jwt.JWTClaimsRegistry(
            iss={"essential": True, "value": SETTINGS.issuer},
            aud={"essential": True, "value": SETTINGS.audience},
            exp={"essential": True},
        ).validate(decoded.claims)
        return VerifiedLocalBearer(decoded.claims, SETTINGS.issuer, SETTINGS.audience)

    return BrowserAuthApplication(
        wire,
        authority,
        graph="fixture-graph",
        origin=ORIGIN,
        issuer=issuer,
        caller=BrowserCallerResolver(authority, verify, graph="fixture-graph"),
    )


@pytest.mark.spec("ARBORA-RELEASE-R003.7")
def test_browser_login_through_the_issuer_reaches_a_verified_session(
    synthetic_signing_key,  # noqa: F811
) -> None:
    async def scenario() -> None:
        wire = Wire()
        app = _application(wire, synthetic_signing_key)
        start, body = await request(
            app, auth_scope(), b'{"username":"fixture","password":"fixture-password"}'
        )
        assert start["status"] == 200
        cookie = dict(start["headers"])[b"set-cookie"].split(b";", 1)[0]
        assert json.loads(body)["csrf_token"]
        # The browser holds a session cookie; no bearer token reaches the page.
        assert len(wire.live) == 1 and next(iter(wire.live)).encode() not in body
        scope = auth_scope()
        scope.update(method="GET", path="/auth/session")
        scope["headers"].append((b"cookie", cookie))
        session, _ = await request(app, scope, b"")
        assert session["status"] == 200
        # Logout revokes the engine session, so the same cookie reads as no session.
        csrf = json.loads(body)["csrf_token"].encode()
        scope.update(method="POST", path="/auth/logout")
        scope["headers"].append((b"x-csrf-token", csrf))
        assert (await request(app, scope, b""))[0]["status"] == 204
        assert not wire.live
        scope.update(method="GET", path="/auth/session")
        assert (await request(app, scope, b""))[0]["status"] == 401

    asyncio.run(scenario())
