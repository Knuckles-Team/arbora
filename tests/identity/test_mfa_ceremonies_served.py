"""Second-factor sign-in ceremonies served over the real ASGI app.

The engine is a structural double of its ``mfa`` family: it holds the credentials, the
one-use challenge, the recovery-code set and every counter, and completes the pending
session on a pass. The assertion is signed with a real P-256 key, so Arbora's verification
is exercised end to end (ARBORA-IDENTITY-R006.2, .2.2, .2.3 and .4).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from arbora.identity import mfa_ceremonies
from arbora.identity.browser import SESSION_COOKIE, csrf_token_for
from arbora.identity.browser_runtime import (
    BrowserAuthApplication,
    BrowserCallerResolver,
)
from arbora.identity.mfa_ceremonies import (
    RECOVERY_MODELS,
    WEBAUTHN_MODELS,
    challenge_id_of,
)
from arbora.identity.webauthn import b64url_encode
from tests.identity.test_browser_runtime import Authority, auth_scope, request
from tests.identity.test_issuer import SETTINGS  # noqa: F401
from tests.identity.test_issuer import (
    synthetic_signing_key as synthetic_signing_key,  # noqa: F401
)
from tests.identity.test_modes_and_browser import ORIGIN, TOKEN

CODES = ["ABCDE12345", "FGHIJ67890"]
CREDENTIAL = "cred-1"


def _cose(public: ec.EllipticCurvePublicKey) -> str:
    point = public.public_numbers()
    x, y = point.x.to_bytes(32, "big"), point.y.to_bytes(32, "big")
    cbor = bytes.fromhex("a50102032620012158 20".replace(" ", "")) + x
    cbor += bytes.fromhex("225820") + y
    return b64url_encode(cbor)


class _Pending(Authority):
    """A session the engine holds pending: it resolves to no caller until a factor passes."""

    def __init__(self) -> None:
        super().__init__()
        self.error = PermissionError("second factor pending")


class _Engine:
    def __init__(self, authority: _Pending) -> None:
        self.authority = authority
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential = {
            "credential_id": CREDENTIAL,
            "name": "key",
            "public_key_cose": _cose(self.key.public_key()),
            "sign_count": 4,
            "transports": ["usb"],
        }
        self.challenge: str | None = None
        self.codes = set(CODES)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.credentials: list[dict[str, Any]] = [self.credential]
        self.audit: list[dict[str, str]] = []

    async def send(self, client: Any, req: Any, graph: str) -> Any:
        from medulla import EngineResponseError

        op, body = req["op"]["op"], req["op"]["request"]
        self.calls.append((op, body))
        value: Any = {"ok": True}
        if op == "webauthn_credentials":
            value = list(self.credentials)
        elif op == "issue_challenge":
            self.challenge = body["challenge"]
            value = {
                "challenge": body["challenge"],
                "expires_at_ms": 1,
                "kind": "webauthn",
            }
        elif op == "consume_challenge":
            if self.challenge is None:
                raise EngineResponseError("WEBAUTHN_CHALLENGE_USED")
            value = {
                "challenge": self.challenge,
                "expires_at_ms": 1,
                "kind": "webauthn",
            }
            self.challenge = None
        elif op == "verify_webauthn":
            if body["new_sign_count"] <= self.credential["sign_count"]:
                raise EngineResponseError("WEBAUTHN_COUNTER_REGRESSED")
            self.credential["sign_count"] = body["new_sign_count"]
            self.authority.error = None
        elif op == "consume_recovery_code":
            if body["code"] not in CODES:
                raise EngineResponseError("MFA_CODE_INVALID")
            if body["code"] not in self.codes:
                raise EngineResponseError("RECOVERY_CODE_USED")
            self.codes.discard(body["code"])
            self.authority.error = None
        return SimpleNamespace(payload={"kind": "done", "value": value})

    def sign(
        self, *, count: int, origin: str = ORIGIN, challenge: str | None = None
    ) -> dict[str, str]:
        chal = challenge or self.challenge or ""
        client = json.dumps(
            {"type": "webauthn.get", "challenge": chal, "origin": origin}
        ).encode()
        rp = ORIGIN.split("//")[1]
        data = hashlib.sha256(rp.encode()).digest() + b"\x01" + count.to_bytes(4, "big")
        signature = self.key.sign(
            data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "challenge_id": challenge_id_of(chal),
            "credential_id": CREDENTIAL,
            "client_data_json": b64url_encode(client),
            "authenticator_data": b64url_encode(data),
            "signature": b64url_encode(signature),
        }


class _Issuer:
    async def issue(self, credential: str) -> str:
        return "fixture-jwt"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from medulla.generated import models, security

    for name in (*WEBAUTHN_MODELS, *RECOVERY_MODELS):
        if not hasattr(models, name):
            monkeypatch.setattr(models, name, object, raising=False)
    authority = _Pending()
    engine = _Engine(authority)
    monkeypatch.setattr(security, "send_identity", engine.send)
    monkeypatch.setattr(security, "decode_identity", lambda response: None)
    app = BrowserAuthApplication(
        object(),
        authority,
        graph="fixture-graph",
        origin=ORIGIN,
        issuer=_Issuer(),
        caller=BrowserCallerResolver(
            authority, authority.verify, graph="fixture-graph"
        ),
        audit=engine.audit.append,
    )
    return SimpleNamespace(app=app, engine=engine, authority=authority)


def _post(
    world: SimpleNamespace,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    session: bool = True,
    csrf: bool = True,
) -> tuple[int, dict[str, Any], dict[bytes, bytes]]:
    scope = auth_scope()
    scope["path"] = path
    if session:
        scope["headers"].append((b"cookie", f"{SESSION_COOKIE}={TOKEN}".encode()))
    if session and csrf:
        scope["headers"].append((b"x-csrf-token", csrf_token_for(TOKEN).encode()))
    start, response = asyncio.run(
        request(world.app, scope, json.dumps(body or {}).encode())
    )
    return start["status"], json.loads(response), dict(start["headers"])


def _begin(world: SimpleNamespace) -> dict[str, Any]:
    status, answer, _ = _post(world, mfa_ceremonies.BEGIN)
    assert status == 200
    return answer


@pytest.mark.spec("ARBORA-IDENTITY-R006.4")
def test_webauthn_begin_issues_a_one_use_challenge_bound_to_the_interim_session(
    world: SimpleNamespace,
) -> None:
    answer = _begin(world)
    options = answer["options"]
    assert answer["challenge_id"] == challenge_id_of(options["challenge"])
    assert options["rpId"] == "console.invalid"
    assert options["allowCredentials"] == [
        {"type": "public-key", "id": CREDENTIAL, "transports": ["usb"]}
    ]
    issued = [body for op, body in world.engine.calls if op == "issue_challenge"]
    assert issued == [
        {
            "challenge": options["challenge"],
            "kind": "webauthn",
            "session_token": TOKEN,
        }
    ]
    # The challenge is valid once: a second finish finds none.
    first = world.engine.sign(count=5)
    assert _post(world, mfa_ceremonies.FINISH, first)[0] == 200
    again = _post(world, mfa_ceremonies.FINISH, first)
    assert again[0] == 403 and again[1] == {"error": "WEBAUTHN_CHALLENGE_USED"}
    # It is bound to a pending session cookie: no cookie, no proof, no challenge.
    before = len(world.engine.calls)
    assert _post(world, mfa_ceremonies.BEGIN, session=False)[0] == 401
    assert _post(world, mfa_ceremonies.BEGIN, csrf=False)[0] == 401
    assert len(world.engine.calls) == before
    # A person with no registered credential has nothing to assert.
    world.engine.credentials.clear()
    status, answer, _ = _post(world, mfa_ceremonies.BEGIN)
    assert (status, answer) == (403, {"error": "WEBAUTHN_NO_CREDENTIAL"})


@pytest.mark.spec("ARBORA-IDENTITY-R006.2.2")
def test_webauthn_finish_checks_origin_and_counter(world: SimpleNamespace) -> None:
    _begin(world)
    wrong_origin = world.engine.sign(count=5, origin="https://evil.invalid")
    assert _post(world, mfa_ceremonies.FINISH, wrong_origin)[:2] == (
        403,
        {"error": "WEBAUTHN_ORIGIN_MISMATCH"},
    )
    assert world.authority.error is not None
    # A counter that does not rise is refused, and the credential's counter is unchanged.
    _begin(world)
    assert _post(world, mfa_ceremonies.FINISH, world.engine.sign(count=4))[:2] == (
        403,
        {"error": "WEBAUTHN_COUNTER_REGRESSED"},
    )
    assert world.engine.credential["sign_count"] == 4
    # A signature by another key is refused before the engine records anything.
    _begin(world)
    forged = world.engine.sign(count=6)
    forged["signature"] = b64url_encode(
        ec.generate_private_key(ec.SECP256R1()).sign(b"x", ec.ECDSA(hashes.SHA256()))
    )
    assert _post(world, mfa_ceremonies.FINISH, forged)[:2] == (
        403,
        {"error": "WEBAUTHN_SIGNATURE_INVALID"},
    )
    assert not [c for c in world.engine.calls if c[0] == "verify_webauthn"]
    # The right origin and a rising counter complete the login with a full session.
    _begin(world)
    status, answer, headers = _post(
        world, mfa_ceremonies.FINISH, world.engine.sign(count=5)
    )
    assert status == 200 and answer["outcome"] == "ok" and "csrf_token" in answer
    assert b"Secure" in headers[b"set-cookie"]
    assert world.engine.credential["sign_count"] == 5
    assert {"method": "webauthn", "outcome": "ok"} in world.engine.audit
    assert {"method": "webauthn", "outcome": "WEBAUTHN_ORIGIN_MISMATCH"} in (
        world.engine.audit
    )
    # A body with an extra or missing field reaches no engine.
    before = len(world.engine.calls)
    bad = world.engine.sign(count=9)
    assert _post(world, mfa_ceremonies.FINISH, {**bad, "x": "1"})[0] == 400
    assert _post(world, mfa_ceremonies.FINISH, {"credential_id": "c"})[0] == 400
    assert len(world.engine.calls) == before


@pytest.mark.spec("ARBORA-IDENTITY-R006.2.3")
def test_recovery_codes_are_single_use_and_enrollment_is_enforced(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, answer, _ = _post(world, mfa_ceremonies.RECOVERY, {"code": CODES[0]})
    assert status == 200 and answer["outcome"] == "ok"
    # Each code is valid once: the engine refuses its reuse and the session stays pending.
    world.authority.error = PermissionError("second factor pending")
    reused = _post(world, mfa_ceremonies.RECOVERY, {"code": CODES[0]})
    assert reused[:2] == (403, {"error": "RECOVERY_CODE_USED"})
    unknown = _post(world, mfa_ceremonies.RECOVERY, {"code": "ZZZZZ00000"})
    assert unknown[:2] == (403, {"error": "MFA_CODE_INVALID"})
    # A malformed code never reaches the engine.
    before = len(world.engine.calls)
    for code in ("short", "ABCDE1234!", "ABCDE123456"):
        assert _post(world, mfa_ceremonies.RECOVERY, {"code": code})[0] == 400
    assert len(world.engine.calls) == before
    assert {"method": "recovery", "outcome": "RECOVERY_CODE_USED"} in world.engine.audit

    # A member of a required group who has no factor stays incomplete: the login answers an
    # interim cookie that is never resolved to a caller, and the session is not revoked.
    async def authenticate(client: Any, graph: str, operation: Any, kind: str) -> Any:
        return {"outcome": "mfa_enrollment_required"}

    monkeypatch.setattr("arbora.identity.approval_engine.identity_value", authenticate)
    resolved = len(world.authority.calls)
    login = asyncio.run(
        request(
            world.app,
            auth_scope(),
            b'{"username":"u","password":"fixture-password"}',
        )
    )
    headers = dict(login[0]["headers"])
    body = json.loads(login[1])
    assert login[0]["status"] == 403
    assert body["error"] == "MFA_ENROLLMENT_REQUIRED" and "csrf_token" in body
    assert b"Max-Age=300" in headers[b"set-cookie"]
    assert len(world.authority.calls) == resolved


@pytest.mark.spec("ARBORA-IDENTITY-R006.2.3")
def test_a_second_factor_login_answers_an_interim_cookie_that_grants_nothing(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def authenticate(client: Any, graph: str, operation: Any, kind: str) -> Any:
        return {"outcome": "mfa_required"}

    monkeypatch.setattr("arbora.identity.approval_engine.identity_value", authenticate)
    login = asyncio.run(
        request(
            world.app,
            auth_scope(),
            b'{"username":"u","password":"fixture-password"}',
        )
    )
    assert login[0]["status"] == 200
    assert json.loads(login[1])["outcome"] == "mfa_required"
    assert b"Max-Age=300" in dict(login[0]["headers"])[b"set-cookie"]
    assert world.authority.calls == []


@pytest.mark.spec("ARBORA-IDENTITY-R006.2")
def test_totp_webauthn_recovery_and_enforced_enrollment(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One pending session passes by a WebAuthn assertion, another by a recovery code."""
    _begin(world)
    assert _post(world, mfa_ceremonies.FINISH, world.engine.sign(count=7))[0] == 200
    world.authority.error = PermissionError("second factor pending")
    assert _post(world, mfa_ceremonies.RECOVERY, {"code": CODES[1]})[0] == 200
    assert _post(world, mfa_ceremonies.RECOVERY, {"code": CODES[1]})[0] == 403
    # The engine's lockout is returned unchanged with the time it lifts.
    from medulla import EngineResponseError

    async def locked(client: Any, req: Any, graph: str) -> Any:
        raise EngineResponseError("MFA_LOCKED", "retry_at_ms=1800000000000")

    monkeypatch.setattr("medulla.generated.security.send_identity", locked)
    status, answer, _ = _post(world, mfa_ceremonies.RECOVERY, {"code": CODES[0]})
    assert (status, answer) == (
        429,
        {"error": "MFA_LOCKED", "retry_at_ms": 1800000000000},
    )


@pytest.mark.spec("ARBORA-IDENTITY-R006.2.2")
def test_pinned_engine_refuses_the_webauthn_routes_naming_the_missing_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medulla.generated import models

    for name in WEBAUTHN_MODELS:
        monkeypatch.delattr(models, name, raising=False)
    authority = _Pending()
    app = BrowserAuthApplication(
        object(),
        authority,
        graph="g",
        origin=ORIGIN,
        issuer=_Issuer(),
        caller=BrowserCallerResolver(authority, authority.verify, graph="g"),
    )
    shell = SimpleNamespace(app=app)
    for path, body in ((mfa_ceremonies.BEGIN, {}),):
        status, answer, _ = _post(shell, path, body)
        assert status == 503
        assert answer == {"error": "UNAVAILABLE", "missing": WEBAUTHN_MODELS[0]}
    assert hasattr(mfa_ceremonies, "WEBAUTHN_MODELS")
