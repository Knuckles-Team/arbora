"""Engine-backed session authority; absent qualified record facts refuse."""

from .engine import IdentityUnavailable
from .ports import CredentialState, SessionState


class UnavailableIdentityAuthority:
    """Never fabricate missing source expiry or live session evidence."""

    async def resolve_credential(self, credential: str) -> CredentialState:
        raise IdentityUnavailable("qualified EG credential expiry binding unavailable")

    async def resolve_session(self, credential: str) -> SessionState:
        raise IdentityUnavailable("qualified EG session evidence binding unavailable")


class EngineSessionAuthority:
    """Use an injected trusted broker client; never create or elevate credentials."""

    def __init__(self, client: object, *, graph: str, refresh: bool = True) -> None:
        from .engine import _text

        if type(refresh) is not bool:
            raise ValueError("session refresh must be boolean")
        self._refresh = refresh
        self._graph = _text(graph)
        if not callable(getattr(client, "_send", None)):
            raise IdentityUnavailable("trusted generated engine client required")
        self._client = client

    async def resolve_session(self, credential: str) -> SessionState:
        import asyncio
        import time

        from .engine import Resolution

        if (
            type(credential) is not str
            or not credential
            or len(credential) > 16384
            or any(
                char.isspace() or ord(char) < 32 or ord(char) == 127
                for char in credential
            )
        ):
            raise PermissionError("invalid session credential")
        try:
            from epistemic_graph.client import EngineResponseError
            from epistemic_graph.generated.security import (
                decode_identity,
                send_identity,
            )

            async with asyncio.timeout(1):
                try:
                    response = await send_identity(
                        self._client,
                        {
                            "op": {
                                "family": "session",
                                "op": "resolve",
                                "request": {
                                    "session_token": credential,
                                    **({"refresh": False} if not self._refresh else {}),
                                },
                            }
                        },
                        self._graph,
                    )
                except EngineResponseError as error:
                    # Only the presented session's authoritative absence is a
                    # credential refusal. Broker/policy/transport failures are
                    # unavailable, and diagnostic detail is never authority.
                    if error.code == "IDENTITY_NOT_FOUND":
                        raise PermissionError("session authority refused") from None
                    raise
                decode_identity(response)
                # The generated model validates the contract, but may coerce
                # values. Preserve original wire types for authority parsing.
                resolution = Resolution.from_reply(response.payload)
        except PermissionError:
            raise
        except Exception:
            raise IdentityUnavailable("qualified engine session unavailable") from None
        record = resolution.session_record
        if record is None:
            raise IdentityUnavailable("persisted session evidence unavailable")
        expiry = min(record.absolute_expires_at_ms, record.idle_expires_at_ms)
        now = int(time.time() * 1000)
        if (
            resolution.is_bootstrap
            or expiry <= now
            or (record.mfa_at_ms is not None and record.mfa_at_ms > now)
        ):
            raise PermissionError("session authority refused")
        return SessionState(resolution, expiry, record.session_ref, record.mfa_at_ms)

    async def resolve_credential(self, credential: str) -> CredentialState:
        state = await self.resolve_session(credential)
        return CredentialState(state.resolution, state.expires_at_ms)
