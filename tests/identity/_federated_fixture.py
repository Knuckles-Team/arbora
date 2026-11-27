"""A structural double of the engine ``token`` and ``external_login`` shapes (PIN-LAG).

It holds what the engine holds: the sealed login state with its expiry on the engine
clock, the registered assertion ids and the opened sessions. Arbora holds none of it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import pytest

from arbora.identity.browser_runtime import (
    BrowserAuthApplication,
    BrowserCallerResolver,
)
from arbora.identity.federated import (
    LOGIN_STATE_MODELS,
    Federation,
    oidc_providers,
    saml_providers,
)
from tests.identity.test_browser_runtime import Authority
from tests.identity.test_modes_and_browser import ORIGIN

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
ISSUER = "https://idp.invalid/realm"
CLIENT_ID = "arbora-console"
OIDC_CONFIG = json.dumps(
    [
        {
            "id": "corp",
            "issuer": ISSUER,
            "client_id": CLIENT_ID,
            "secret_ref": "env://CORP_SECRET",
            "jit": "create",
            "rules": [
                {"claim_match": "groups=contractors", "jit": "deny"},
                {"claim_match": "groups=staff"},
            ],
        }
    ]
)
SAML_CONFIG = json.dumps(
    [
        {
            "id": "saml1",
            "entity_id": "urn:arbora:sp",
            "acs_url": ORIGIN + "/auth/saml/acs",
            "idp_certificate_ref": "env://SAML_CERT",
            "jit": "create",
            "rules": [{"claim_match": "role=staff"}],
        }
    ]
)
SECRETS = {
    "env://CORP_SECRET": "client-secret",
    "env://SAML_CERT": "-----BEGIN CERTIFICATE-----",
}


class Engine:
    """Login state, assertion ids and sessions, with expiry on its own clock."""

    def __init__(self) -> None:
        self.now_ms = 0
        self.states: dict[str, dict[str, Any]] = {}
        self.assertions: dict[str, int] = {}
        self.logins: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.last: dict[str, Any] = {}

    async def send(self, client: Any, req: Any, graph: str) -> Any:
        from medulla import EngineResponseError

        family, op, body = req["op"]["family"], req["op"]["op"], req["op"]["request"]
        self.calls.append((family, op))
        kind: str = "done"
        value: Any = {"changed": True}
        if op == "register_login_state":
            assert body["ttl_ms"] <= 600_000
            self.states[body["state"]] = {
                **body,
                "expires": self.now_ms + body["ttl_ms"],
            }
            self.last = body
            kind, value = "login_state_sealed", {"sealed": "opaque"}
        elif op == "consume_login_state":
            held = self.states.pop(body["state"], None)
            if held is None or held["expires"] <= self.now_ms:
                raise EngineResponseError("IDENTITY_TOKEN_SPENT")
            kind = "login_state"
            value = {"nonce": held["nonce"], "pkce_verifier": held["pkce_verifier"]}
        elif op == "register_assertion":
            if body["assertion_id"] in self.assertions:
                raise EngineResponseError("IDENTITY_REPLAY")
            self.assertions[body["assertion_id"]] = body["not_on_or_after_ms"]
        elif op == "external_login":
            self.logins.append(body)
            kind, value = "authenticate", {"outcome": "ok"}
        return SimpleNamespace(payload={"kind": kind, "value": value})


class Reader:
    """A scripted SAML reader: it returns what the test put under the response text."""

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.valid = True
        self.verified = 0

    def read(self, response_b64: str) -> Any:
        if response_b64 not in self.responses:
            raise ValueError("not a SAML response")
        return self.responses[response_b64]

    def verify(self, assertion: Any, idp_certificate_pem: str) -> bool:
        self.verified += 1
        assert "BEGIN CERTIFICATE" in idp_certificate_pem
        return self.valid


class Issuer:
    async def issue(self, credential: str) -> str:
        return "fixture-jwt"


class Transport:
    """The provider: endpoints and the code exchange, over the engine's registered nonce."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.claims: dict[str, Any] = {}
        self.exchanged: list[dict[str, Any]] = []
        self.refuse = False

    async def authorization_endpoint(self, issuer: str) -> str:
        return ISSUER + "/authorize"

    async def end_session_endpoint(self, issuer: str) -> str | None:
        return ISSUER + "/logout"

    async def exchange(self, issuer: str, **kwargs: Any) -> Any:
        self.exchanged.append(kwargs)
        if self.refuse:
            raise PermissionError("the provider refused the code")
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "exp": NOW.timestamp() + 300,
            "nonce": self.engine.last["nonce"],
            "sub": "alice-subject",
            "preferred_username": "alice",
            "groups": ["staff"],
            **self.claims,
        }
        return SimpleNamespace(id_token="signed.id.token", claims=claims)


@pytest.fixture(name="world")
def fed_world(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The served auth app with every engine shape present (the structural double)."""
    from medulla.generated import models

    for name in (*LOGIN_STATE_MODELS, "IdentityOpTokenRegisterAssertion"):
        monkeypatch.setattr(models, name, object, raising=False)
    return _world(monkeypatch)


@pytest.fixture(name="readerless")
def fed_readerless(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Every engine shape present, but no SAML reader installed."""
    from medulla.generated import models

    for name in (*LOGIN_STATE_MODELS, "IdentityOpTokenRegisterAssertion"):
        monkeypatch.setattr(models, name, object, raising=False)
    return _world(monkeypatch, reader=None)


@pytest.fixture(name="lagged")
def fed_lagged(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The same app against the pinned release, which lacks the three token shapes."""
    from medulla.generated import models

    for name in (*LOGIN_STATE_MODELS, "IdentityOpTokenRegisterAssertion"):
        monkeypatch.delattr(models, name, raising=False)
    return _world(monkeypatch)


def _world(monkeypatch: pytest.MonkeyPatch, reader: Any = ...) -> SimpleNamespace:
    from medulla.generated import security

    engine = Engine()
    transport = Transport(engine)
    reader = Reader() if reader is ... else reader
    audit: list[dict[str, str]] = []
    monkeypatch.setattr(security, "send_identity", engine.send)
    monkeypatch.setattr(security, "decode_identity", lambda response: None)
    federation = Federation(
        object(),
        "fixture-graph",
        ORIGIN,
        oidc=oidc_providers(OIDC_CONFIG),
        saml=saml_providers(SAML_CONFIG),
        transport=transport,
        saml_reader=reader,
        resolve=SECRETS.get,
        clock=lambda: NOW,
        audit=audit.append,
    )
    authority = Authority()
    app = BrowserAuthApplication(
        object(),
        authority,
        graph="fixture-graph",
        origin=ORIGIN,
        issuer=Issuer(),
        caller=BrowserCallerResolver(
            authority, authority.verify, graph="fixture-graph"
        ),
        audit=audit.append,
        federation=federation,
    )
    return SimpleNamespace(
        app=app,
        engine=engine,
        transport=transport,
        reader=reader,
        audit=audit,
        federation=federation,
    )


def get(
    world: SimpleNamespace, path: str, query: dict[str, str], cookies: str = ""
) -> Any:
    """One top-level GET navigation; answers ``(status, headers, body)``."""
    headers = [(b"host", b"console.invalid")]
    if cookies:
        headers.append((b"cookie", cookies.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": urlencode(query).encode(),
        "scheme": "https",
        "headers": headers,
    }
    return _run(world, scope, b"")


def post_form(world: SimpleNamespace, path: str, form: dict[str, str]) -> Any:
    """An IdP's cross-site form post: its own origin, no CSRF token."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": b"",
        "scheme": "https",
        "headers": [
            (b"host", b"console.invalid"),
            (b"origin", b"https://idp.invalid"),
            (b"content-type", b"application/x-www-form-urlencoded"),
        ],
    }
    return _run(world, scope, urlencode(form).encode())


def post_json(world: SimpleNamespace, path: str, cookies: str) -> Any:
    from tests.identity.test_browser_runtime import auth_scope

    scope = auth_scope()
    scope["path"] = path
    scope["headers"].append((b"cookie", cookies.encode()))
    from arbora.identity.browser import csrf_token_for
    from tests.identity.test_modes_and_browser import TOKEN

    scope["headers"].append((b"x-csrf-token", csrf_token_for(TOKEN).encode()))
    return _run(world, scope, b"{}")


def _run(world: SimpleNamespace, scope: dict[str, Any], body: bytes) -> Any:
    messages: list[Any] = []

    async def receive() -> Any:
        return {"type": "http.request", "body": body}

    async def send(message: Any) -> None:
        messages.append(message)

    asyncio.run(world.app(scope, receive, send))
    start = messages[0]
    data = b"".join(m.get("body", b"") for m in messages[1:])
    return (
        start["status"],
        [(k.decode(), v.decode()) for k, v in start["headers"]],
        data,
    )


def cookie_values(headers: list[tuple[str, str]]) -> list[str]:
    return [value for name, value in headers if name.lower() == "set-cookie"]
