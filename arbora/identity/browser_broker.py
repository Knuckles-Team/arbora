"""Existing-secret, freshly verified service authority for native Identity RPCs."""

import asyncio
from typing import Any

from .engine import IdentityUnavailable


class ConfiguredBrowserBroker:
    """Never provision a broker, reuse System authority, or cache token material."""

    def __init__(
        self, reference: str, *, tenant: str, audience: str, process_claims: Any
    ):
        from scion_sdk.credentials.runtime_references import (
            validate_runtime_secret_reference,
        )

        try:
            self._reference = validate_runtime_secret_reference(reference)
        except Exception:
            raise IdentityUnavailable(
                "existing browser broker reference required"
            ) from None
        self._tenant, self._audience = tenant, audience
        self._process_claims = process_claims

    async def __call__(self) -> dict[str, Any]:
        from scion_sdk.credentials.runtime_references import (
            resolve_runtime_secret_reference,
        )

        from arbora.security.actor_context import ActorType
        from arbora.security.request_identity import (
            actor_from_bearer_token,
            mint_graph_session,
        )

        try:
            token = await asyncio.to_thread(
                resolve_runtime_secret_reference, self._reference
            )
            actor = await actor_from_bearer_token(token, require_direct_service=True)
            del token
            if actor.actor_type != ActorType.AUTOMATED_SERVICE:
                raise PermissionError("service broker required")
            session = mint_graph_session(actor)
            claims = session.engine_verified_context()
            process = self._process_claims()
            if (
                claims["principal"] == process["principal"]
                or claims["tenant"] != self._tenant
                or claims["audience"] != self._audience
                or process["tenant"] != self._tenant
                or process["audience"] != self._audience
                or "identity:authenticate" not in claims["scopes"]
                or claims["delegation"]
            ):
                raise PermissionError("exact distinct service broker required")
            # Native Identity still verifies this managed principal's current
            # role grants on each RPC; verified JWT claims never replace it.
            return claims
        except asyncio.CancelledError:
            raise
        except Exception:
            # Secret adapters may chain private reference/transport exceptions.
            # Neither exception text nor reference/token contents leave here.
            raise IdentityUnavailable(
                "configured browser broker authority unavailable"
            ) from None
