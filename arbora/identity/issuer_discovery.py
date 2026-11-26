"""Discovery and public keys of the local issuer (ARBORA-IDENTITY-R003).

The signing key ring is an existing secret: ``{"active": <private jwk>, "previous":
<private jwk with retired_at_ms>}``. Only public members are ever served. During the
rotation overlap the JWKS lists both keys; after it only the active one. The issuer
refuses to serve, and so refuses to start, when the ring cannot be read.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from .engine import IdentityUnavailable
from .issuer import ISSUER_KEY_RING_PATH, IssuerSettings
from .ports import SigningKeyStore

DEFAULT_OVERLAP_S = 86_400
OVERLAP_RANGE_S = (60, 604_800)
_PRIVATE_MEMBERS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})
_REQUIRED = ("kty", "kid", "n", "e")


def bounded_overlap(value: int | None) -> int:
    """``identity.issuer.rotation_overlap_s``: default 86,400, range [60, 604,800]."""
    if value is None:
        return DEFAULT_OVERLAP_S
    low, high = OVERLAP_RANGE_S
    if type(value) is not int or not low <= value <= high:
        raise ValueError("rotation_overlap_s must be within [60, 604800] seconds")
    return value


def _public(key: Mapping[str, Any]) -> dict[str, Any]:
    if any(not isinstance(key.get(name), str) or not key[name] for name in _REQUIRED):
        raise IdentityUnavailable("issuer key is not a public-capable RSA key")
    return {
        **{k: v for k, v in key.items() if k not in _PRIVATE_MEMBERS},
        "use": "sig",
        "alg": "RS256",
    }


class IssuerDiscovery:
    """The OIDC discovery document and the JWKS of one local issuer."""

    def __init__(
        self,
        store: SigningKeyStore,
        settings: IssuerSettings,
        *,
        rotation_overlap_s: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._settings = settings
        self._overlap_ms = bounded_overlap(rotation_overlap_s) * 1000
        self._clock = clock
        self.jwks()  # ISSUER_KEY_UNAVAILABLE: the issuer refuses to start without its ring.

    def jwks(self) -> dict[str, Any]:
        stored = self._store.get(ISSUER_KEY_RING_PATH)
        try:
            if not isinstance(stored, str) or not stored:
                raise ValueError("ring")
            ring: dict[str, Any] = json.loads(stored)
            keys = [_public(ring["active"])]
            previous = ring.get("previous")
            if previous is not None:
                retired = previous["retired_at_ms"]
                if type(retired) is not int:
                    raise ValueError("retired_at_ms")
                if int(self._clock() * 1000) < retired + self._overlap_ms:
                    keys.append(_public(previous))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise IdentityUnavailable("ISSUER_KEY_UNAVAILABLE") from None
        return {"keys": keys}

    def document(self) -> dict[str, Any]:
        issuer = self._settings.issuer.rstrip("/")
        return {
            "issuer": self._settings.issuer,
            "jwks_uri": f"{issuer}/.well-known/jwks.json",
            "id_token_signing_alg_values_supported": ["RS256"],
            "subject_types_supported": ["public"],
            "response_types_supported": ["code"],
        }

    def asgi(self) -> Any:
        """The two read-only discovery routes."""
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        def respond(build: Callable[[], dict[str, Any]]) -> Callable[[Any], Any]:
            async def handler(request: Any) -> Any:
                try:
                    body = build()
                except IdentityUnavailable:
                    return JSONResponse({"error": "ISSUER_KEY_UNAVAILABLE"}, 503)
                return JSONResponse(body, headers={"Cache-Control": "max-age=60"})

            return handler

        return Starlette(
            routes=[
                Route("/.well-known/openid-configuration", respond(self.document)),
                Route("/.well-known/jwks.json", respond(self.jwks)),
            ]
        )
