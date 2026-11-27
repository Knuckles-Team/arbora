"""Engine-backed identity administration port (ARBORA-IDENTITY-R018.1).

One call sends one engine ``Identity`` request over the caller-bound client and
returns the reply value. Arbora holds no identity record; every answer is the
engine's own. An engine refusal is passed on as its wire code so a handler can
name it for the console.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from .engine import IdentityUnavailable, _text


class IdentityRefused(Exception):
    """The engine refused an identity request with this wire code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class EngineIdentityAdmin:
    """Sends ``Identity`` and ``AuditVerify`` requests for the admin operations."""

    def __init__(self, *, graph: str, timeout_s: float = 5.0) -> None:
        self._graph = _text(graph)
        self._timeout = timeout_s

    async def identity(
        self,
        client: Any,
        claims: Mapping[str, Any],
        family: str,
        op: str,
        request: Mapping[str, Any] | None = None,
        *,
        expect: str,
    ) -> Any:
        """Send one ``Identity`` op and return the value of the ``expect`` reply."""
        from medulla.client import EngineResponseError
        from medulla.generated.security import decode_identity, send_identity

        from arbora.api.invoke.steps import claim_values
        from arbora.security.anonymous import client_scope

        operation: dict[str, Any] = {"family": family, "op": op}
        if request is not None:
            operation["request"] = dict(request)
        try:
            async with asyncio.timeout(self._timeout):
                with client_scope(client, claim_values(claims)):
                    response = await send_identity(
                        client, {"op": operation}, self._graph
                    )
            decode_identity(response)
        except EngineResponseError as error:
            if str(error.code).startswith(("IDENTITY_", "MFA_")) or error.code in {
                "ACCESS_DENIED",
                "CONFLICT",
            }:
                raise IdentityRefused(str(error.code)) from None
            raise IdentityUnavailable("identity operation unavailable") from None
        except Exception:
            raise IdentityUnavailable("identity operation unavailable") from None
        payload = response.payload
        if not isinstance(payload, Mapping) or payload.get("kind") != expect:
            raise IdentityUnavailable("identity response kind disagrees")
        return payload["value"]

    async def audit_verify(self, client: Any, claims: Mapping[str, Any]) -> Any:
        """Read the engine audit chain verdict (``AuditVerify``)."""
        from medulla.generated.security import decode_audit_verify, send_audit_verify

        from arbora.api.invoke.steps import claim_values
        from arbora.security.anonymous import client_scope

        try:
            async with asyncio.timeout(self._timeout):
                with client_scope(client, claim_values(claims)):
                    response = await send_audit_verify(client, {}, self._graph)
            report = decode_audit_verify(response)
        except Exception:
            raise IdentityUnavailable("audit verification unavailable") from None
        return report.model_dump(mode="json")
