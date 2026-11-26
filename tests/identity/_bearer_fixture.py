"""A local issuer for the bearer tests: one RSA key, a JWKS and a token minter."""

from __future__ import annotations

from typing import Any

from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from arbora.security.bearer import BearerVerificationProfile

NOW = 1_800_000_000
ISSUER = "https://issuer.example.test"
AUDIENCE = "arbora-api"
JWKS_URI = "https://issuer.example.test/jwks"

_KEY = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig", "alg": "RS256"})
_OTHER = RSAKey.generate_key(
    2048, parameters={"kid": "k1", "use": "sig", "alg": "RS256"}
)
JWKS: dict[str, Any] = KeySet([_KEY]).as_dict(private=False)
PROFILE = BearerVerificationProfile(
    issuer=ISSUER, audience=AUDIENCE, key_authority=JWKS_URI
)


def claims(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "usr:ada",
        "tenant_id": "tenant:one",
        "client_id": "cli:one",
        "scope": "kg:read kg:write",
        "iat": NOW,
        "nbf": NOW,
        "exp": NOW + 300,
    }
    base.update(changes)
    return {key: value for key, value in base.items() if value is not None}


def mint(forged: bool = False, **changes: Any) -> str:
    key = _OTHER if forged else _KEY
    return jwt.encode({"alg": "RS256", "kid": "k1"}, claims(**changes), key)
