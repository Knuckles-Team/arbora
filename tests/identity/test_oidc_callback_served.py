"""The OIDC code flow served over the real auth app (ARBORA-IDENTITY-R009.2.3).

The engine is a structural double of its ``token`` and ``external_login`` shapes; the
pinned release lacks the login-state operations (PIN-LAG), so the pin-lag test asserts
the route answers ``UNAVAILABLE`` naming the missing shape and calls nothing.
"""

from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from arbora.identity.browser import SESSION_COOKIE
from arbora.identity.federated import HINT_COOKIE, OIDC_CALLBACK, OIDC_LOGIN
from tests.identity._federated_fixture import (
    ISSUER,
    cookie_values,
    get,
    post_json,
)
from tests.identity.test_modes_and_browser import ORIGIN, TOKEN

UNIFORM = b'{"error":"Authentication required"}'


def _start(world: SimpleNamespace) -> dict[str, str]:
    status, headers, _ = get(world, OIDC_LOGIN, {"provider": "corp"})
    assert status == 303
    location = dict(headers)["location"]
    assert location.startswith(ISSUER + "/authorize?")
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def _callback(world: SimpleNamespace, state: str, code: str = "code-1"):
    return get(world, OIDC_CALLBACK, {"state": state, "code": code})


@pytest.mark.pin_lag
@pytest.mark.spec("ARBORA-IDENTITY-R009.2.3")
def test_pkce_state_nonce_mapping_and_hinted_logout(world: SimpleNamespace) -> None:
    query = _start(world)
    held = world.engine.states[query["state"]]
    # The engine holds the state, nonce and verifier; the browser gets a S256 challenge.
    digest = hashlib.sha256(held["pkce_verifier"].encode()).digest()
    assert (
        query["code_challenge"]
        == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    )
    assert query["code_challenge_method"] == "S256" and query["nonce"] == held["nonce"]
    assert query["redirect_uri"] == ORIGIN + OIDC_CALLBACK and held["ttl_ms"] == 600_000
    assert len(held["pkce_verifier"]) == 64
    status, headers, _ = _callback(world, query["state"])
    # The callback exchanged the code with the verifier the engine released.
    assert world.transport.exchanged[0]["verifier"] == held["pkce_verifier"]
    assert world.transport.exchanged[0]["client_secret"] == "client-secret"
    assert status == 303 and dict(headers)["location"] == ORIGIN + "/"
    assert world.engine.logins[0]["idp_id"] == "corp"
    assert world.engine.logins[0]["subject"] == "alice-subject"
    assert world.engine.logins[0]["claims"]["groups"] == ["staff"]
    cookies = cookie_values(headers)
    assert any(c.startswith(SESSION_COOKIE + "=") for c in cookies)
    hint = next(c for c in cookies if c.startswith(HINT_COOKIE + "="))
    assert "HttpOnly" in hint and "Secure" in hint
    assert world.audit[-1] == {
        "protocol": "oidc",
        "provider": "corp",
        "rule": "1",
        "outcome": "ok",
    }
    # Logout carries the stored provider's end-session URL with the ID token hint.
    value = hint.split(";")[0]
    status, _, body = post_json(
        world, "/auth/logout", f"{SESSION_COOKIE}={TOKEN}; {value}"
    )
    logout = urlsplit(__import__("json").loads(body)["end_session_url"])
    assert status == 200 and logout.geturl().startswith(ISSUER + "/logout?")
    fields = parse_qs(logout.query)
    assert fields["id_token_hint"] == ["signed.id.token"]
    assert fields["post_logout_redirect_uri"] == [ORIGIN + "/"]


@pytest.mark.pin_lag
@pytest.mark.spec("ARBORA-IDENTITY-R009.2.3")
def test_every_refusal_is_the_same_401_and_the_state_is_single_use(
    world: SimpleNamespace,
) -> None:
    answers: dict[str, tuple[int, bytes]] = {}

    def refused(name: str, state: str, **kwargs: str) -> None:
        status, headers, body = _callback(world, state, **kwargs)
        assert not cookie_values(headers)
        answers[name] = (status, body)

    refused("unknown", "corp.never-registered")
    state = _start(world)["state"]
    assert _callback(world, state)[0] == 303
    refused("used", state)
    state = _start(world)["state"]
    world.engine.now_ms += 600_001
    refused("expired", state)
    state = _start(world)["state"]
    world.transport.claims = {"nonce": "another-nonce"}
    refused("nonce", state)
    world.transport.claims = {}
    state = _start(world)["state"]
    world.transport.refuse = True
    refused("provider", state)
    world.transport.refuse = False
    state = _start(world)["state"]
    world.transport.claims = {"groups": ["nobody"]}
    refused("no-rule", state)
    state = _start(world)["state"]
    world.transport.claims = {"groups": ["contractors", "staff"]}
    refused("deny", state)
    assert set(answers.values()) == {(401, UNIFORM)}
    # The precise reason lives in the audit event only.
    reasons = [e["outcome"] for e in world.audit if e["outcome"].startswith("OIDC_")]
    assert reasons == [
        "OIDC_STATE_MISMATCH",
        "OIDC_STATE_MISMATCH",
        "OIDC_STATE_MISMATCH",
        "OIDC_NONCE_MISMATCH",
        "OIDC_TOKEN_INVALID",
        "OIDC_NO_RULE_MATCH",
        "OIDC_JIT_DENIED",
    ]
    assert len(world.engine.logins) == 1


@pytest.mark.pin_lag
@pytest.mark.spec("ARBORA-IDENTITY-R009.2.3")
def test_unknown_provider_and_malformed_callback_are_refused_alike(
    world: SimpleNamespace,
) -> None:
    assert get(world, OIDC_LOGIN, {"provider": "nope"})[0] == 401
    assert get(world, OIDC_LOGIN, {})[0] == 401
    assert get(world, OIDC_CALLBACK, {"state": "corp.x"})[0] == 401
    assert (
        get(world, OIDC_CALLBACK, {"state": "corp.x", "error": "access_denied"})[0]
        == 401
    )
    assert world.engine.calls == []


@pytest.mark.pin_lag
@pytest.mark.spec("ARBORA-IDENTITY-R009.2.3")
def test_pinned_release_without_login_state_answers_unavailable(
    lagged: SimpleNamespace,
) -> None:
    status, _, body = get(lagged, OIDC_LOGIN, {"provider": "corp"})
    assert status == 503 and b"IdentityOpTokenRegisterLoginState" in body
    status, _, body = get(lagged, OIDC_CALLBACK, {"state": "corp.x", "code": "c"})
    assert status == 503 and b"UNAVAILABLE" in body
    assert lagged.engine.calls == [] and not lagged.engine.logins
