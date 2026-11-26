"""TOTP routes pass through to the engine (ARBORA-IDENTITY-R006.2.1).

The engine holds the secret and checks the code; the routes forward the verified session,
its tenant and the code, and return the engine's answer. The refusal test runs under the
pinned engine, which lacks the four ``mfa`` shapes; the real-path tests install structural
doubles of engine ``main``'s shapes and of the identity sender.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.identity import mfa_totp
from arbora.identity.browser import SESSION_COOKIE, csrf_token_for
from arbora.identity.browser_runtime import (
    BrowserAuthApplication,
    BrowserCallerResolver,
)
from arbora.identity.mfa_totp import TOTP_MODELS
from tests.identity.test_browser_runtime import Authority, auth_scope, request
from tests.identity.test_issuer import SETTINGS  # noqa: F401
from tests.identity.test_issuer import (
    synthetic_signing_key as synthetic_signing_key,  # noqa: F401
)
from tests.identity.test_modes_and_browser import ORIGIN, TOKEN

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R006.2.1")


def _pinned_has_models() -> bool:
    from medulla.generated import models

    return all(hasattr(models, name) for name in TOTP_MODELS)


class _Issuer:
    async def issue(self, credential: str) -> str:
        return "fixture-jwt"


def _app() -> BrowserAuthApplication:
    authority = Authority()
    return BrowserAuthApplication(
        object(),
        authority,
        graph="fixture-graph",
        origin=ORIGIN,
        issuer=_Issuer(),
        caller=BrowserCallerResolver(
            authority, authority.verify, graph="fixture-graph"
        ),
    )


def _scope(path: str, *, session: bool = True, csrf: bool = True) -> dict[str, Any]:
    scope = auth_scope()
    scope["path"] = path
    if session:
        scope["headers"].append((b"cookie", f"{SESSION_COOKIE}={TOKEN}".encode()))
    if session and csrf:
        scope["headers"].append((b"x-csrf-token", csrf_token_for(TOKEN).encode()))
    return scope


class _Engine:
    """Structural double of the identity sender: records each ``mfa`` call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.refuse: Exception | None = None
        self.value: dict[str, Any] = {"ok": True}

    async def send(self, client: Any, request: Any, graph: str) -> Any:
        self.calls.append(request["op"])
        if self.refuse is not None:
            raise self.refuse
        return SimpleNamespace(payload={"kind": "done", "value": self.value})


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> _Engine:
    from medulla.generated import models, security

    for name in TOTP_MODELS:
        monkeypatch.setattr(models, name, object, raising=False)
    double = _Engine()
    monkeypatch.setattr(security, "send_identity", double.send)
    monkeypatch.setattr(security, "decode_identity", lambda response: None)
    return double


def _post(
    path: str, body: dict[str, Any] | None = None, **scope: Any
) -> tuple[Any, Any]:
    start, response = asyncio.run(
        request(_app(), _scope(path, **scope), json.dumps(body or {}).encode())
    )
    return start["status"], json.loads(response)


@pytest.mark.skipif(_pinned_has_models(), reason="the pin carries the mfa shapes")
def test_pinned_engine_refuses_every_route_naming_the_missing_shape() -> None:
    for path, body in (
        ("/auth/mfa/totp/enroll", {}),
        ("/auth/mfa/totp/confirm", {"code": "123456"}),
        ("/auth/mfa/totp/verify", {"code": "123456"}),
        ("/auth/mfa/totp/disable", {"code": "123456"}),
    ):
        status, answer = _post(path, body)
        assert status == 503
        assert answer == {"error": "UNAVAILABLE", "missing": "IdentityOpMfaEnroll"}


def test_each_route_forwards_session_tenant_and_code_and_returns_the_engine_answer(
    engine: _Engine,
) -> None:
    engine.value = {"provisioning_uri": "otpauth://totp/x?secret=ENGINEHELD"}
    status, answer = _post("/auth/mfa/totp/enroll")
    assert status == 200 and answer == engine.value
    for path, code in (
        ("/auth/mfa/totp/confirm", "111111"),
        ("/auth/mfa/totp/verify", "222222"),
        ("/auth/mfa/totp/disable", "333333"),
    ):
        engine.value = {"ok": True}
        assert _post(path, {"code": code}) == (200, {"ok": True})
    assert [c["op"] for c in engine.calls] == ["enroll", "confirm", "verify", "disable"]
    assert {c["family"] for c in engine.calls} == {"mfa"}
    assert engine.calls[0]["request"] == {"tenant": "fixture-tenant", "session": TOKEN}
    assert engine.calls[2]["request"] == {
        "tenant": "fixture-tenant",
        "session": TOKEN,
        "code": "222222",
    }


def test_replay_invalid_and_lockout_are_the_engine_refusals(engine: _Engine) -> None:
    from medulla import EngineResponseError

    for code, public in (
        ("MFA_CODE_REPLAYED", "IDENTITY_REPLAY"),
        ("MFA_CODE_INVALID", "IDENTITY_NOT_FOUND"),
    ):
        engine.refuse = EngineResponseError(code)
        assert _post("/auth/mfa/totp/verify", {"code": "123456"}) == (
            403,
            {"error": public},
        )
    engine.refuse = EngineResponseError("MFA_LOCKED", "retry_at_ms=1800000000000")
    # A lockout is no oracle: it answers like any other unknown proof.
    assert _post("/auth/mfa/totp/verify", {"code": "123456"}) == (
        403,
        {"error": "IDENTITY_NOT_FOUND"},
    )
    engine.refuse = EngineResponseError("SOMETHING_ELSE", "diagnostic")
    status, answer = _post("/auth/mfa/totp/verify", {"code": "123456"})
    assert (status, answer) == (503, {"error": "UNAVAILABLE"})
    engine.refuse = ConnectionError("down")
    assert _post("/auth/mfa/totp/verify", {"code": "123456"})[0] == 503


def test_no_session_no_proof_or_a_bad_code_reaches_no_engine(engine: _Engine) -> None:
    assert _post("/auth/mfa/totp/verify", {"code": "123456"}, session=False)[0] == 401
    assert _post("/auth/mfa/totp/verify", {"code": "123456"}, csrf=False)[0] == 401
    for body in ({}, {"code": "12345"}, {"code": "abcdef"}, {"code": "123456", "x": 1}):
        assert _post("/auth/mfa/totp/verify", body)[0] == 400
    assert _post("/auth/mfa/totp/enroll", {"code": "123456"})[0] == 400
    assert engine.calls == []


def test_arbora_holds_no_secret_or_counter() -> None:
    source = open(mfa_totp.__file__, encoding="utf-8").read()
    for forbidden in ("hmac", "hashlib", "secrets", "pyotp", "time.time"):
        assert forbidden not in source
