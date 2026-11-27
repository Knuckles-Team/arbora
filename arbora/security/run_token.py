"""CONCEPT:MERISTEM-OS.identity.run-scoped-tool-token — Run-Scoped Tool Token.

Assimilated from open-design's ``OD_TOOL_TOKEN``: each run is minted a short-lived, HMAC-signed token
bound to ``run_id`` / ``project`` / an endpoint allowlist / expiry, injected into the run environment.
Tool endpoints derive their scope from the token and reject out-of-scope or expired calls — the daemon
is the sole policy authority.

Superiority delta: the token is derived from the multi-tenant ``ActorContext`` (OS-5.1), so it carries
tenant/role identity in addition to the endpoint allowlist. Pure stdlib (``hmac``); no new deps.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from arbora.core.environment import read_env


class RunTokenSecretUnavailable(RuntimeError):
    """Raised when a run-token signing secret is required but not configured (fail-closed)."""


def token_secret_from_config() -> str | None:
    """The configured run-token signing secret, or ``None`` when none resolves.

    ``ARBORA_TOKEN_SECRET`` resolves from the secrets backend or config. There is
    no process-local fallback: a per-process random secret is state that other
    replicas cannot verify, and a PID-derived one is forgeable.
    """
    return read_env("ARBORA_TOKEN_SECRET")


@dataclass(slots=True)
class RunToken:
    """A decoded run-scoped token."""

    run_id: str
    project: str
    endpoints: tuple[str, ...]
    operations: tuple[str, ...]
    expires_at: float
    actor_id: str = ""
    tenant_id: str = ""


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class TokenError(ValueError):
    """Raised when a token is malformed, tampered, or otherwise invalid."""


class RunTokenSigner:
    """Mints and validates run tokens under one host's signing secret.

    The host builds one signer from the authority it owns and passes it by argument;
    two hosts with different secrets refuse each other's tokens. The secret is read
    through ``secret_source`` at each use, so a deployment that rotates it through the
    secrets backend takes effect, and a missing one fails closed.
    """

    __slots__ = ("_secret_source",)

    def __init__(self, secret_source: Callable[[], str | None]) -> None:
        self._secret_source = secret_source

    def _secret(self) -> bytes:
        val = self._secret_source()
        if not val:
            raise RunTokenSecretUnavailable(
                "ARBORA_TOKEN_SECRET must resolve from the secrets backend to mint or "
                "validate a run token; no process-local fallback secret exists. Configure "
                "the secret reference (e.g. a vault KV path)."
            )
        return val.encode()

    def _sign(self, body: str) -> str:
        return _b64e(hmac.new(self._secret(), body.encode(), hashlib.sha256).digest())

    def require_configured(self) -> None:
        """Fail closed at startup when delegated identity is on but no secret resolves.

        A no-op in ``off``/``warn`` mode.
        """
        from arbora.security.delegation import DelegationMode, delegation_mode

        if delegation_mode() is DelegationMode.ON and not self._secret_source():
            raise RunTokenSecretUnavailable(
                "ENABLE_DELEGATED_IDENTITY=on requires ARBORA_TOKEN_SECRET to resolve "
                "from the secrets backend before startup (no PID-derived fallback exists)."
            )

    def mint(
        self,
        run_id: str,
        *,
        project: str = "",
        endpoints: tuple[str, ...] = ("*",),
        operations: tuple[str, ...] = ("read",),
        ttl_seconds: float = 3600.0,
        actor_id: str = "",
        tenant_id: str = "",
        now: float | None = None,
    ) -> str:
        """Mint a signed run-scoped token string (``<payload>.<sig>``)."""
        t = time.time() if now is None else now
        payload = {
            "run_id": run_id,
            "project": project,
            "endpoints": list(endpoints),
            "operations": list(operations),
            "expires_at": t + ttl_seconds,
            "actor_id": actor_id,
            "tenant_id": tenant_id,
        }
        body = _b64e(json.dumps(payload, sort_keys=True).encode())
        # ".".join(...) rather than an f-string: the ``<payload>.<sig>`` token
        # format (never a query) -- this two-part dotted shape is otherwise
        # indistinguishable from a schema-qualified table cast at the AST level.
        return ".".join((body, self._sign(body)))

    def decode(self, token: str) -> RunToken:
        """Verify the signature and decode a token. Raises :class:`TokenError`."""
        try:
            body, sig = token.split(".", 1)
        except ValueError as exc:
            raise TokenError("malformed token") from exc
        if not hmac.compare_digest(sig, self._sign(body)):
            raise TokenError("bad signature")
        data = json.loads(_b64d(body))
        return RunToken(
            run_id=data["run_id"],
            project=data.get("project", ""),
            endpoints=tuple(data.get("endpoints", ())),
            operations=tuple(data.get("operations", ())),
            expires_at=float(data["expires_at"]),
            actor_id=data.get("actor_id", ""),
            tenant_id=data.get("tenant_id", ""),
        )

    def validate(
        self,
        token: str,
        *,
        endpoint: str | None = None,
        operation: str | None = None,
        now: float | None = None,
    ) -> RunToken:
        """Decode and enforce expiry and the allowlists. Raises :class:`TokenError`."""
        decoded = self.decode(token)
        t = time.time() if now is None else now
        if t >= decoded.expires_at:
            raise TokenError("token expired")
        if (
            endpoint is not None
            and "*" not in decoded.endpoints
            and endpoint not in decoded.endpoints
        ):
            raise TokenError(f"endpoint {endpoint!r} not in token scope")
        if (
            operation is not None
            and "*" not in decoded.operations
            and operation not in decoded.operations
        ):
            raise TokenError(f"operation {operation!r} not in token scope")
        return decoded
