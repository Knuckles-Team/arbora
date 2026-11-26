"""WebAuthn assertion verification (ARBORA-IDENTITY-R006.2.2).

Pure functions over bytes. The engine holds the credentials (public key, signature
counter) and the one-use challenge; this module checks one assertion against what the
engine returned: the ceremony type, the challenge, the exact public origin, the relying
party hash, user presence, the signature and a strictly rising counter. It keeps no state.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa


class WebauthnRefused(Exception):
    """One closed refusal of an assertion; ``code`` is the served error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


#: Authenticator-data flag bit: the user was present.
USER_PRESENT = 0x01
_COSE_KTY, _COSE_ALG, _COSE_CRV = 1, 3, -1
_COSE_X, _COSE_Y, _COSE_N, _COSE_E = -2, -3, -1, -2
_AUTH_DATA_MIN = 37


def b64url_decode(value: Any) -> bytes:
    """Strict unpadded or padded base64url; anything else is an invalid assertion."""
    if not isinstance(value, str) or not value:
        raise WebauthnRefused("WEBAUTHN_ASSERTION_INVALID")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeEncodeError):
        raise WebauthnRefused("WEBAUTHN_ASSERTION_INVALID") from None


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _cbor_item(data: bytes, at: int, depth: int = 0) -> tuple[Any, int]:
    """One CBOR item of the subset a COSE key uses: ints, byte and text strings, maps."""
    if depth > 4 or at >= len(data):
        raise WebauthnRefused("WEBAUTHN_ASSERTION_INVALID")
    major, info = data[at] >> 5, data[at] & 0x1F
    at += 1
    if info < 24:
        value = info
    elif info in (24, 25, 26):
        size = {24: 1, 25: 2, 26: 4}[info]
        if at + size > len(data):
            raise WebauthnRefused("WEBAUTHN_ASSERTION_INVALID")
        value = int.from_bytes(data[at : at + size], "big")
        at += size
    else:
        raise WebauthnRefused("WEBAUTHN_ASSERTION_INVALID")
    if major == 0:
        return value, at
    if major == 1:
        return -1 - value, at
    if major in (2, 3):
        if at + value > len(data):
            raise WebauthnRefused("WEBAUTHN_ASSERTION_INVALID")
        raw = data[at : at + value]
        return (raw if major == 2 else raw.decode("utf-8", "replace")), at + value
    if major == 5:
        found: dict[Any, Any] = {}
        for _ in range(value):
            key, at = _cbor_item(data, at, depth + 1)
            found[key], at = _cbor_item(data, at, depth + 1)
        return found, at
    raise WebauthnRefused("WEBAUTHN_ASSERTION_INVALID")


def cose_key(public_key_cose: str) -> dict[Any, Any]:
    """The COSE key map of the engine's base64url credential public key."""
    data = b64url_decode(public_key_cose)
    key, end = _cbor_item(data, 0)
    if not isinstance(key, dict) or end != len(data):
        raise WebauthnRefused("WEBAUTHN_ASSERTION_INVALID")
    return key


def _verify_signature(key: dict[Any, Any], signature: bytes, signed: bytes) -> None:
    """Check ``signature`` over ``signed`` with ES256, EdDSA or RS256 keys."""
    try:
        algorithm = key.get(_COSE_ALG)
        if algorithm == -7 and key.get(_COSE_KTY) == 2 and key.get(_COSE_CRV) == 1:
            point = b"\x04" + key[_COSE_X] + key[_COSE_Y]
            public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
            public.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
        elif algorithm == -8 and key.get(_COSE_KTY) == 1 and key.get(_COSE_CRV) == 6:
            ed25519.Ed25519PublicKey.from_public_bytes(key[_COSE_X]).verify(
                signature, signed
            )
        elif algorithm == -257 and key.get(_COSE_KTY) == 3:
            numbers = rsa.RSAPublicNumbers(
                int.from_bytes(key[_COSE_E], "big"), int.from_bytes(key[-1], "big")
            )
            numbers.public_key().verify(
                signature, signed, padding.PKCS1v15(), hashes.SHA256()
            )
        else:
            raise WebauthnRefused("WEBAUTHN_ASSERTION_INVALID")
    except InvalidSignature:
        raise WebauthnRefused("WEBAUTHN_SIGNATURE_INVALID") from None
    except (KeyError, TypeError, ValueError):
        raise WebauthnRefused("WEBAUTHN_ASSERTION_INVALID") from None


@dataclass(frozen=True, slots=True)
class Assertion:
    """The three byte strings of one authentication response."""

    client_data_json: bytes
    authenticator_data: bytes
    signature: bytes


def verify_assertion(
    assertion: Assertion,
    *,
    challenge: str,
    origin: str,
    rp_id: str,
    public_key_cose: str,
    stored_sign_count: int,
) -> int:
    """Verify one assertion and return its signature counter.

    Refuses ``WEBAUTHN_ORIGIN_MISMATCH`` for any origin but ``origin``,
    ``WEBAUTHN_CHALLENGE_MISMATCH`` for a challenge the engine did not issue,
    ``WEBAUTHN_SIGNATURE_INVALID`` for a signature the stored key rejects, and
    ``WEBAUTHN_COUNTER_REGRESSED`` unless the counter is strictly above
    ``stored_sign_count``.
    """
    try:
        client = json.loads(assertion.client_data_json)
    except (ValueError, UnicodeError):
        raise WebauthnRefused("WEBAUTHN_ASSERTION_INVALID") from None
    if not isinstance(client, dict) or client.get("type") != "webauthn.get":
        raise WebauthnRefused("WEBAUTHN_ASSERTION_INVALID")
    if client.get("origin") != origin or client.get("crossOrigin") is True:
        raise WebauthnRefused("WEBAUTHN_ORIGIN_MISMATCH")
    if client.get("challenge") != challenge:
        raise WebauthnRefused("WEBAUTHN_CHALLENGE_MISMATCH")
    data = assertion.authenticator_data
    if len(data) < _AUTH_DATA_MIN:
        raise WebauthnRefused("WEBAUTHN_ASSERTION_INVALID")
    if data[:32] != hashlib.sha256(rp_id.encode("ascii")).digest():
        raise WebauthnRefused("WEBAUTHN_ORIGIN_MISMATCH")
    if not data[32] & USER_PRESENT:
        raise WebauthnRefused("WEBAUTHN_ASSERTION_INVALID")
    signed = data + hashlib.sha256(assertion.client_data_json).digest()
    _verify_signature(cose_key(public_key_cose), assertion.signature, signed)
    counter = int.from_bytes(data[33:37], "big")
    if counter <= stored_sign_count:
        raise WebauthnRefused("WEBAUTHN_COUNTER_REGRESSED")
    return counter
