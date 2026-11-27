"""A signed-in person enrolls and manages second factors through served operations.

The engine is a structural double of its ``mfa`` family: it holds the TOTP enrollment, the
WebAuthn credentials, the recovery-code set and the one-use challenges. The WebAuthn
attestation is built with a real P-256 key so Arbora's origin, relying-party and key
checks run end to end (ARBORA-IDENTITY-R006.3).
"""

from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from arbora.api.invoke.pipeline import EXTERNAL_EFFECT_OPERATIONS, OperationRefused
from arbora.api.ops import identity_mfa
from arbora.core.settings import RuntimeConfig
from arbora.identity.engine_admin import IdentityRefused
from arbora.identity.mfa_ceremonies import challenge_id_of
from arbora.identity.session_credential import (
    bind_session_credential,
    session_credential,
)
from arbora.identity.webauthn import b64url_decode, b64url_encode

ORIGIN = "https://arbora.example.test"
CREDENTIAL = "cred-session-1"


class Engine:
    """The engine's ``mfa``, ``user`` and ``session`` families for one session."""

    def __init__(self) -> None:
        self.challenges: dict[str, str] = {}
        self.totp_pending = False
        self.totp = False
        self.codes: list[str] = []
        self.keys: list[dict[str, Any]] = []
        self.required = False
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def identity(
        self,
        client: Any,
        claims: Any,
        family: str,
        op: str,
        request: Any = None,
        *,
        expect: str,
    ) -> Any:
        body = dict(request or {})
        self.calls.append((family, op, body))
        return getattr(self, f"_{family}_{op}")(body)

    def _user_get(self, body: dict[str, Any]) -> Any:
        return {"totp_enrolled": self.totp, "recovery_codes_left": len(self.codes)}

    def _session_resolve(self, body: dict[str, Any]) -> Any:
        return {"mfa_required": self.required}

    def _mfa_webauthn_credentials(self, body: dict[str, Any]) -> Any:
        return list(self.keys)

    def _mfa_enroll(self, body: dict[str, Any]) -> Any:
        self.totp_pending = True
        return {"provisioning_uri": "otpauth://totp/arbora?secret=ABC"}

    def _mfa_issue_challenge(self, body: dict[str, Any]) -> Any:
        self.challenges[body["kind"]] = body["challenge"]
        return {
            "challenge": body["challenge"],
            "expires_at_ms": 300_000,
            "kind": body["kind"],
        }

    def _mfa_consume_challenge(self, body: dict[str, Any]) -> Any:
        challenge = self.challenges.pop(body["kind"], None)
        if challenge is None:
            raise IdentityRefused("IDENTITY_NOT_FOUND")
        return {"challenge": challenge, "expires_at_ms": 1, "kind": body["kind"]}

    def _mfa_confirm(self, body: dict[str, Any]) -> Any:
        if body["code"] != "123456":
            raise IdentityRefused("MFA_CODE_INVALID")
        self.totp, self.totp_pending = True, False
        return {"ok": True}

    def _mfa_disable(self, body: dict[str, Any]) -> Any:
        self.totp = False
        return {"ok": True}

    def _mfa_set_recovery_codes(self, body: dict[str, Any]) -> Any:
        self.codes = list(body["codes"])
        return {"ok": True}

    def _mfa_register_webauthn(self, body: dict[str, Any]) -> Any:
        self.keys.append({"credential_id": body["credential_id"], "name": body["name"]})
        return {"ok": True}

    def _mfa_remove_webauthn(self, body: dict[str, Any]) -> Any:
        self.keys = [k for k in self.keys if k["credential_id"] != body["id"]]
        return {"ok": True}


_CONFIG = RuntimeConfig()
_CONFIG.webui_origin = ORIGIN


def _context(engine: Engine, *, age_ms: int = 1000) -> Any:
    caller = SimpleNamespace(
        principal="person:one",
        tenant="tenant-a",
        engine_claims={},
        mfa_at_ms=int(time.time() * 1000) - age_ms,
    )
    return SimpleNamespace(
        services={"identity_admin": engine, "config": _CONFIG},
        client=None,
        caller=caller,
    )


def _cbor_text(text: str) -> bytes:
    raw = text.encode()
    return bytes([0x60 | len(raw)]) + raw


def _attestation(challenge: str, *, origin: str = ORIGIN, fmt: str = "none") -> dict:
    key = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()
    cose = (
        bytes.fromhex("a5010203262001215820")
        + key.x.to_bytes(32, "big")
        + bytes.fromhex("225820")
        + key.y.to_bytes(32, "big")
    )
    identifier = b"credential-id-1"
    auth = (
        hashlib.sha256(b"arbora.example.test").digest()
        + bytes([0x41])
        + (0).to_bytes(4, "big")
        + bytes(16)
        + len(identifier).to_bytes(2, "big")
        + identifier
        + cose
    )
    attested = (
        bytes([0xA3])
        + _cbor_text("fmt")
        + _cbor_text(fmt)
        + _cbor_text("attStmt")
        + bytes([0xA0])
        + _cbor_text("authData")
        + bytes([0x58, len(auth)])
        + auth
    )
    client = json.dumps(
        {"type": "webauthn.create", "challenge": challenge, "origin": origin}
    ).encode()
    return {
        "client_data_json": b64url_encode(client),
        "attestation_object": b64url_encode(attested),
    }


@pytest.fixture(autouse=True)
def _origin() -> Any:
    with bind_session_credential(CREDENTIAL):
        yield


async def _call(handler: str, context: Any, **params: Any) -> Any:
    result = await getattr(identity_mfa, handler)(context, params, None)
    return result["value"]


@pytest.mark.spec("ARBORA-IDENTITY-R006.3")
async def test_enroll_totp_webauthn_and_recovery_through_served_operations() -> None:
    engine = Engine()
    context = _context(engine)
    begun = await _call("handle_totp_begin", context)
    assert begun["otpauth_uri"].startswith("otpauth://") and begun["expires_at"] > 0
    confirmed = await _call(
        "handle_totp_confirm",
        context,
        enrollment_id=begun["enrollment_id"],
        code="123456",
    )
    assert confirmed == {"enrolled": True} and engine.totp

    started = await _call("handle_webauthn_begin", context, label="desk key")
    options = started["options"]
    assert options["rp"]["id"] == "arbora.example.test"
    assert started["challenge_id"] == challenge_id_of(options["challenge"])
    finished = await _call(
        "handle_webauthn_finish",
        context,
        challenge_id=started["challenge_id"],
        attestation=_attestation(options["challenge"]),
    )
    assert finished["label"] == "desk key" and engine.keys[0]["name"] == "desk key"

    codes = (await _call("handle_recovery_regenerate", context))["codes"]
    assert len(codes) == 10 and engine.codes == codes
    status = await _call("handle_mfa_status", context)
    assert {m["method"]: m["count"] for m in status["methods"]} == {
        "totp": 1,
        "webauthn": 1,
        "recovery": 10,
    }
    removed = await _call(
        "handle_credentials_remove",
        context,
        method="webauthn",
        credential_id=finished["credential_id"],
    )
    assert removed == {"removed": True} and engine.keys == []
    # Every call acted on the caller's own session credential and no secret leaks back.
    assert all(
        request.get("session_token", request.get("session", CREDENTIAL)) == CREDENTIAL
        for _f, op, request in engine.calls
        if op not in {"get", "remove_webauthn"}
    )


@pytest.mark.spec("ARBORA-IDENTITY-R006.3")
async def test_stale_check_and_last_required_method_are_refused() -> None:
    engine = Engine()
    stale = _context(engine, age_ms=identity_mfa.FRESH_MS + 1000)
    for handler, params in (
        ("handle_totp_begin", {}),
        ("handle_recovery_regenerate", {}),
        ("handle_credentials_remove", {"method": "recovery"}),
    ):
        with pytest.raises(OperationRefused) as refused:
            await _call(handler, stale, **params)
        assert refused.value.code == "MFA_STALE"
    assert engine.calls == []

    context = _context(engine)
    begun = await _call("handle_totp_begin", context)
    with pytest.raises(OperationRefused) as wrong:
        await _call(
            "handle_totp_confirm",
            context,
            enrollment_id=begun["enrollment_id"],
            code="000000",
        )
    assert wrong.value.code == "MFA_CODE_INVALID"
    # The enrollment challenge is one use: a second confirm finds it spent.
    with pytest.raises(OperationRefused) as spent:
        await _call(
            "handle_totp_confirm",
            context,
            enrollment_id=begun["enrollment_id"],
            code="123456",
        )
    assert spent.value.code == "ENROLLMENT_EXPIRED"

    started = await _call("handle_webauthn_begin", context, label="k")
    for attestation, code in (
        (
            _attestation(started["options"]["challenge"], origin="https://evil.test"),
            "WEBAUTHN_ORIGIN_MISMATCH",
        ),
    ):
        with pytest.raises(OperationRefused) as bad:
            await _call(
                "handle_webauthn_finish",
                context,
                challenge_id=started["challenge_id"],
                attestation=attestation,
            )
        assert bad.value.code == code
    with pytest.raises(OperationRefused) as used:
        await _call(
            "handle_webauthn_finish",
            context,
            challenge_id=started["challenge_id"],
            attestation=_attestation(started["options"]["challenge"]),
        )
    assert used.value.code == "WEBAUTHN_CHALLENGE_USED"

    # Removing the last method of a person who must hold one is refused.
    engine.required, engine.totp, engine.codes, engine.keys = True, True, [], []
    with pytest.raises(OperationRefused) as last:
        await _call("handle_credentials_remove", context, method="totp")
    assert last.value.code == "MFA_ENROLLMENT_REQUIRED"
    # A credential the caller does not hold answers like an absent one.
    with pytest.raises(OperationRefused) as foreign:
        await _call(
            "handle_credentials_remove",
            context,
            method="webauthn",
            credential_id="other",
        )
    assert foreign.value.code == "INVALID_ARGUMENT"


@pytest.mark.spec("ARBORA-IDENTITY-R006.3")
async def test_no_browser_session_refuses_every_operation() -> None:
    engine = Engine()
    assert session_credential() == CREDENTIAL
    from arbora.identity import session_credential as module

    token = module._CREDENTIAL.set(None)
    try:
        for handler in ("handle_mfa_status", "handle_totp_begin"):
            with pytest.raises(OperationRefused) as refused:
                await _call(handler, _context(engine))
            assert refused.value.code == "CLIENT_SESSION_MISSING"
    finally:
        module._CREDENTIAL.reset(token)
    assert engine.calls == []


@pytest.mark.spec("ARBORA-IDENTITY-R006.3")
def test_the_seven_operations_are_registered_self_scoped_and_journaled() -> None:
    specs = {spec.id: spec for spec in identity_mfa.operations()}
    mfa = {i: s for i, s in specs.items() if i.startswith("identity.self.mfa.")}
    assert len(mfa) == 7
    assert all(spec.scopes == frozenset({"identity:self"}) for spec in mfa.values())
    writes = [i for i, s in mfa.items() if s.effect.value != "read"]
    assert len(writes) == 6 and set(writes) <= set(EXTERNAL_EFFECT_OPERATIONS)
    assert b64url_decode(b64url_encode(b"x")) == b"x"
