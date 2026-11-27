"""A signed-in person's second factors through served operations (ARBORA-IDENTITY-R006.3).

Seven operations, all scope ``identity:self``. The engine holds every TOTP secret, WebAuthn
credential, recovery-code hash and one-use challenge; each handler passes the caller's own
session credential (``arbora.identity.session_credential``) to one engine ``mfa`` call and
returns the engine's answer. A person reaches only the factors of the session they hold, so
no operation names another principal. A mutation needs a password or second-factor check no
older than 300 seconds. The WebAuthn attestation is checked here (origin, relying-party
hash, challenge, ``none`` attestation) because the engine sees no client data.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import build_read_op, build_write_op
from arbora.api.ops.identity import _P, SELF_SCOPE, Empty, Value, _send
from arbora.api.registry import OpSpec, PrincipalRule, Verb
from arbora.identity.mfa_ceremonies import RECOVERY_CODE_LENGTH, challenge_id_of
from arbora.identity.session_credential import session_credential
from arbora.identity.webauthn import (
    WebauthnRefused,
    b64url_decode,
    b64url_encode,
    cbor_item,
    cose_key,
)

#: A mutation needs a password or second-factor check no older than this.
FRESH_MS = 300_000
RECOVERY_CODE_COUNT = 10
CHALLENGE_BYTES = 32
_AUTH_DATA_FIXED = 37
_FLAG_ATTESTED = 0x40
_ES256 = -7
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_REFUSALS = MappingProxyType({"MFA_CODE_INVALID": "MFA_CODE_INVALID"})


class TotpConfirm(_P):
    enrollment_id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


class WebauthnBegin(_P):
    label: str = Field(min_length=1, max_length=64)


class Attestation(_P):
    client_data_json: str = Field(min_length=1, max_length=8192)
    attestation_object: str = Field(min_length=1, max_length=8192)


class WebauthnFinish(_P):
    challenge_id: str = Field(min_length=1, max_length=128)
    attestation: Attestation


class CredentialRemove(_P):
    method: Literal["totp", "webauthn", "recovery"]
    credential_id: str | None = Field(default=None, min_length=1, max_length=512)


def _credential() -> str:
    credential = session_credential()
    if credential is None:
        raise OperationRefused("CLIENT_SESSION_MISSING")
    return credential


def _fresh(context: Any) -> str:
    """The caller's credential, refused ``MFA_STALE`` after the freshness bound."""
    credential = _credential()
    at = getattr(context.caller, "mfa_at_ms", None)
    if at is None or not 0 <= int(time.time() * 1000) - int(at) <= FRESH_MS:
        raise OperationRefused("MFA_STALE")
    return credential


async def _mfa(
    context: Any,
    op: str,
    request: Mapping[str, Any],
    *,
    expect: str,
    refusals: Mapping[str, str] | None = None,
) -> Any:
    return await _send(context, "mfa", op, request, expect=expect, refusals=refusals)


async def _factors(context: Any, credential: str) -> dict[str, Any]:
    """The caller's own factor facts: the user row, WebAuthn credentials and the policy."""
    user = await _send(
        context,
        "user",
        "get",
        {"id": context.caller.principal},
        expect="user",
        refusals={"IDENTITY_NOT_FOUND": "USER_NOT_FOUND"},
    )
    keys = await _mfa(
        context,
        "webauthn_credentials",
        {"session_token": credential},
        expect="webauthn_credentials",
    )
    resolved = await _send(
        context,
        "session",
        "resolve",
        {"session_token": credential},
        expect="resolution",
    )
    return {
        "user": user,
        "keys": list(keys),
        "required": bool(resolved["mfa_required"]),
    }


def _methods(facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    user, keys = facts["user"], facts["keys"]
    codes = int(user["recovery_codes_left"])
    totp = bool(user["totp_enrolled"])
    return [
        {"method": "totp", "enrolled": totp, "count": 1 if totp else 0},
        {"method": "webauthn", "enrolled": bool(keys), "count": len(keys)},
        {"method": "recovery", "enrolled": codes > 0, "count": codes},
    ]


async def handle_mfa_status(context: Any, params: Mapping[str, Any], op: OpSpec) -> Any:
    facts = await _factors(context, _credential())
    return {"value": {"methods": _methods(facts), "required": facts["required"]}}


async def handle_totp_begin(context: Any, params: Mapping[str, Any], op: OpSpec) -> Any:
    credential = _fresh(context)
    tenant = context.caller.tenant
    provisioning = await _mfa(
        context,
        "enroll",
        {"session": credential, "tenant": tenant},
        expect="mfa_provisioning",
    )
    enrollment = secrets.token_urlsafe(16)
    view = await _mfa(
        context,
        "issue_challenge",
        {"challenge": enrollment, "kind": "enrollment", "session_token": credential},
        expect="mfa_challenge",
    )
    return {
        "value": {
            "enrollment_id": enrollment,
            "otpauth_uri": provisioning["provisioning_uri"],
            "expires_at": view["expires_at_ms"],
        }
    }


async def _consume(context: Any, kind: str, credential: str, expired: str) -> str:
    """The one-use challenge of ``kind``; a missing, spent or expired one answers alike."""
    view = await _mfa(
        context,
        "consume_challenge",
        {"kind": kind, "session_token": credential},
        expect="mfa_challenge",
        refusals={"IDENTITY_NOT_FOUND": expired},
    )
    return str(view["challenge"])


async def handle_totp_confirm(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    credential = _fresh(context)
    challenge = await _consume(context, "enrollment", credential, "ENROLLMENT_EXPIRED")
    if not hmac.compare_digest(challenge, params["enrollment_id"]):
        raise OperationRefused("ENROLLMENT_EXPIRED")
    await _mfa(
        context,
        "confirm",
        {
            "session": credential,
            "tenant": context.caller.tenant,
            "code": params["code"],
        },
        expect="done",
        refusals=_CODE_REFUSALS,
    )
    return {"value": {"enrolled": True}}


def _relying_party(config: Any) -> tuple[str, str]:
    from urllib.parse import urlsplit

    origin = str(config.webui_origin or "")
    return origin, urlsplit(origin).hostname or ""


async def handle_webauthn_begin(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    credential = _fresh(context)
    origin, rp_id = _relying_party(context.services["config"])
    if not rp_id:
        raise OperationRefused("UNAVAILABLE", {"reason": "browser origin unset"})
    label = params["label"].encode("utf-8")
    challenge = b64url_encode(secrets.token_bytes(CHALLENGE_BYTES) + label)
    await _mfa(
        context,
        "issue_challenge",
        {"challenge": challenge, "kind": "webauthn", "session_token": credential},
        expect="mfa_challenge",
    )
    principal = str(context.caller.principal)
    return {
        "value": {
            "challenge_id": challenge_id_of(challenge),
            "options": {
                "challenge": challenge,
                "rp": {"id": rp_id, "name": "Arbora"},
                "user": {
                    "id": b64url_encode(hashlib.sha256(principal.encode()).digest()),
                    "name": principal,
                    "displayName": principal,
                },
                "pubKeyCredParams": [{"type": "public-key", "alg": _ES256}],
                "attestation": "none",
                "timeout": FRESH_MS,
            },
        }
    }


def _parsed_attestation(
    body: Mapping[str, str], challenge: str, origin: str, rp_id: str
) -> tuple[str, str]:
    """``(credential id, COSE key)`` of a ``none`` attestation for this challenge."""
    client = json.loads(b64url_decode(body["client_data_json"]))
    if client.get("type") != "webauthn.create" or client.get("challenge") != challenge:
        raise WebauthnRefused("WEBAUTHN_ATTESTATION_INVALID")
    if client.get("origin") != origin:
        raise WebauthnRefused("WEBAUTHN_ORIGIN_MISMATCH")
    attested = b64url_decode(body["attestation_object"])
    item, _end = cbor_item(attested, 0)
    if not isinstance(item, dict) or item.get("fmt") != "none":
        raise WebauthnRefused("WEBAUTHN_ATTESTATION_INVALID")
    return _credential_of(item.get("authData"), rp_id)


def _credential_of(auth_data: Any, rp_id: str) -> tuple[str, str]:
    if not isinstance(auth_data, bytes) or len(auth_data) < _AUTH_DATA_FIXED + 18:
        raise WebauthnRefused("WEBAUTHN_ATTESTATION_INVALID")
    if auth_data[:32] != hashlib.sha256(rp_id.encode()).digest():
        raise WebauthnRefused("WEBAUTHN_ORIGIN_MISMATCH")
    if not auth_data[32] & _FLAG_ATTESTED:
        raise WebauthnRefused("WEBAUTHN_ATTESTATION_INVALID")
    at = _AUTH_DATA_FIXED + 16
    size = int.from_bytes(auth_data[at : at + 2], "big")
    identifier = auth_data[at + 2 : at + 2 + size]
    start = at + 2 + size
    _key, end = cbor_item(auth_data, start)
    cose = b64url_encode(auth_data[start:end])
    if cose_key(cose).get(3) != _ES256 or not identifier:
        raise WebauthnRefused("WEBAUTHN_ATTESTATION_INVALID")
    return b64url_encode(identifier), cose


async def handle_webauthn_finish(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    credential = _fresh(context)
    challenge = await _consume(
        context, "webauthn", credential, "WEBAUTHN_CHALLENGE_USED"
    )
    if challenge_id_of(challenge) != params["challenge_id"]:
        raise OperationRefused("WEBAUTHN_CHALLENGE_USED")
    origin, rp_id = _relying_party(context.services["config"])
    try:
        identifier, cose = _parsed_attestation(
            params["attestation"], challenge, origin, rp_id
        )
    except WebauthnRefused as refused:
        raise OperationRefused(_attestation_code(refused.code)) from None
    except (ValueError, UnicodeError):
        raise OperationRefused("WEBAUTHN_ATTESTATION_INVALID") from None
    label = b64url_decode(challenge)[CHALLENGE_BYTES:].decode("utf-8", "replace")
    await _mfa(
        context,
        "register_webauthn",
        {
            "credential_id": identifier,
            "name": label,
            "public_key_cose": cose,
            "session_token": credential,
            "sign_count": 0,
            "transports": [],
        },
        expect="done",
    )
    return {"value": {"credential_id": identifier, "label": label}}


def _attestation_code(code: str) -> str:
    known = {"WEBAUTHN_ORIGIN_MISMATCH", "WEBAUTHN_ATTESTATION_INVALID"}
    return code if code in known else "WEBAUTHN_ATTESTATION_INVALID"


def _recovery_codes() -> list[str]:
    return [
        "".join(secrets.choice(_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
        for _ in range(RECOVERY_CODE_COUNT)
    ]


async def handle_recovery_regenerate(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    credential = _fresh(context)
    codes = _recovery_codes()
    await _mfa(
        context,
        "set_recovery_codes",
        {"codes": codes, "session_token": credential},
        expect="done",
    )
    return {"value": {"codes": codes}}


def _would_leave_none(facts: Mapping[str, Any], method: str) -> bool:
    """Whether removing ``method`` leaves a required person with no second factor."""
    left = {m["method"]: m["count"] for m in _methods(facts)}
    left[method] = max(0, left[method] - 1) if method == "webauthn" else 0
    return bool(facts["required"]) and sum(left.values()) == 0


async def handle_credentials_remove(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    credential = _fresh(context)
    method, identifier = params["method"], params.get("credential_id")
    facts = await _factors(context, credential)
    owned = {key["credential_id"] for key in facts["keys"]}
    if method == "webauthn" and identifier not in owned:
        # A foreign credential answers exactly like an absent one.
        raise OperationRefused("INVALID_ARGUMENT", {"field": "credential_id"})
    if _would_leave_none(facts, method):
        raise OperationRefused("MFA_ENROLLMENT_REQUIRED")
    await _remove(context, credential, method, identifier)
    return {"value": {"removed": True}}


async def _remove(
    context: Any, credential: str, method: str, identifier: str | None
) -> None:
    if method == "webauthn":
        await _mfa(context, "remove_webauthn", {"id": identifier}, expect="done")
    elif method == "totp":
        await _mfa(
            context,
            "disable",
            {"session": credential, "tenant": context.caller.tenant},
            expect="done",
            refusals=_CODE_REFUSALS,
        )
    else:
        await _mfa(
            context,
            "set_recovery_codes",
            {"codes": [], "session_token": credential},
            expect="done",
        )


#: id -> (handler, params, summary, write)
_TABLE: tuple[tuple[str, str, type[_P], str, bool], ...] = (
    ("identity.self.mfa.status", "handle_mfa_status", Empty, "Read the caller's enrolled second factors", False),
    ("identity.self.mfa.totp.begin", "handle_totp_begin", Empty, "Begin enrolling an authenticator app", True),
    ("identity.self.mfa.totp.confirm", "handle_totp_confirm", TotpConfirm, "Confirm an authenticator app with a code", True),
    ("identity.self.mfa.webauthn.register_begin", "handle_webauthn_begin", WebauthnBegin, "Begin registering a security key", True),
    ("identity.self.mfa.webauthn.register_finish", "handle_webauthn_finish", WebauthnFinish, "Finish registering a security key", True),
    ("identity.self.mfa.recovery.regenerate", "handle_recovery_regenerate", Empty, "Replace the recovery codes", True),
    ("identity.self.mfa.credentials.remove", "handle_credentials_remove", CredentialRemove, "Remove a second factor", True),
)  # fmt: skip

#: The mutating operation ids; the effect journal settles each at most once per request.
MUTATING = tuple(op_id for op_id, _h, _p, _s, write in _TABLE if write)


def operations() -> tuple[OpSpec, ...]:
    """Reads are plain; each mutation needs an idempotency key and is audited."""
    specs: list[OpSpec] = []
    for op_id, handler, params, summary, write in _TABLE:
        common: dict[str, Any] = {
            "op_id": op_id,
            "summary": summary,
            "examples": (summary,),
            "params": params,
            "result": Value,
            "handler": f"arbora.api.ops.identity_mfa.{handler}",
            "scope": SELF_SCOPE,
            "principals": PrincipalRule.HUMAN_UNDELEGATED,
        }
        specs.append(
            build_write_op(verb=Verb.MANAGE, **common)
            if write
            else build_read_op(**common)
        )
    return tuple(specs)


specs = operations
