"""Delegated identity for connector calls (ARBORA-IDENTITY-R028.1 to R028.5).

A connector tool may require an acting user (``requires_actor``). Arbora then exchanges the
verified caller's token for one short token whose audience is the connector, in process
(the token-exchange grant of RFC 8693), and a process credential alone never satisfies such
a tool. This type is distinct from ``SpawnDelegation`` in ``arbora/security/delegation.py``.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from arbora.security.audit_event import audit_event

logger = logging.getLogger(__name__)

__all__ = [
    "EXCHANGE_MAX_SECONDS",
    "REQUIRES_ACTOR_META_KEY",
    "DelegatedIdentity",
    "DelegationRefused",
    "exchange_for_actor",
    "requires_actor",
    "resolve_actor_exchange_bearer",
]

#: The ``tools/list`` ``_meta`` key a connector sets (``SCION-CONNECTOR-CONTROL-R902.1``).
REQUIRES_ACTOR_META_KEY = "team.knuckles/requires_actor"
EXCHANGE_MAX_SECONDS = 300


class DelegationRefused(Exception):
    """A refusal; its text is the code alone, never a token."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def requires_actor(tool_meta: Mapping[str, Any] | None) -> bool:
    """The flag from ``tools/list`` ``_meta`` only; absent or non-true is false."""
    if not isinstance(tool_meta, Mapping):
        return False
    return tool_meta.get(REQUIRES_ACTOR_META_KEY) is True


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise DelegationRefused("DELEGATION_FIELD_INVALID")
    return value


@dataclass(frozen=True, slots=True)
class DelegatedIdentity:
    subject: str
    tenant: str
    audience: str
    scopes: tuple[str, ...]
    expires_at: int
    exchange_id: str

    def __post_init__(self) -> None:
        for name in ("subject", "tenant", "audience", "exchange_id"):
            _text(getattr(self, name))
        if type(self.expires_at) is not int or self.expires_at <= 0:
            raise DelegationRefused("DELEGATION_FIELD_INVALID")
        object.__setattr__(
            self, "scopes", tuple(sorted({_text(scope) for scope in self.scopes}))
        )


async def exchange_for_actor(
    caller: Any,
    connector_client_id: str,
    *,
    issuer: Any,
    claims: Mapping[str, Any],
) -> tuple[DelegatedIdentity, str]:
    """Exchange the verified caller token for one scoped to the connector.

    ``issuer.exchange(claims, audience)`` answers the new token and its expiry; the
    lifetime is the smaller of 300 s and the remaining caller lifetime, and an expired
    caller token refuses.
    """
    audience = _text(connector_client_id)
    token, expires_at, exchange_id = await issuer.exchange(claims, audience)
    identity = DelegatedIdentity(
        subject=str(caller.principal),
        tenant=str(caller.tenant),
        audience=audience,
        scopes=tuple(sorted(caller.effective_scopes)),
        expires_at=int(expires_at),
        exchange_id=exchange_id,
    )
    return identity, token


async def resolve_actor_exchange_bearer(
    server: str,
    tool: str,
    *,
    needs_actor: bool,
    caller: Any,
    claims: Mapping[str, Any] | None,
    connector_client_id: str,
    issuer: Any,
    emit: Callable[[Mapping[str, str]], None],
) -> dict[str, str] | None:
    """The ``Authorization`` header of an actor-required forward, else ``None``.

    A tool without the flag keeps the process credential. An actor-required call
    without a verified user caller refuses ``IDENTITY_DELEGATION_REQUIRED``; a failed
    exchange refuses ``IDENTITY_EXCHANGE_FAILED``. A refusal makes no connector call. The
    one audit event names subject, audience, exchange id and ``server/tool``, never a token.
    """
    if not needs_actor:
        return None
    if (
        caller is None
        or getattr(caller, "principal_kind", "") not in ("user", "human")
        or not getattr(caller, "authenticated", False)
        or not claims
    ):
        raise DelegationRefused("IDENTITY_DELEGATION_REQUIRED")
    try:
        identity, token = await exchange_for_actor(
            caller, connector_client_id, issuer=issuer, claims=claims
        )
    except Exception:  # one code for every failed exchange
        logger.warning("credential authority call failed", exc_info=True)
        raise DelegationRefused("IDENTITY_EXCHANGE_FAILED") from None
    emit(
        audit_event(
            SimpleNamespace(id="fleet.call", executor=SimpleNamespace(value="caller")),
            {"server": server, "tool": tool},
            caller,
            SimpleNamespace(value="fleet"),
            "ok",
            evidence={
                "delegated_subject": identity.subject,
                "audience": identity.audience,
                "exchange_id": identity.exchange_id,
            },
        )
    )
    return {"Authorization": f"Bearer {token}"}


def new_exchange_id() -> str:
    return secrets.token_hex(16)


def remaining_seconds(claims: Mapping[str, Any], now: float | None = None) -> int:
    expiry = claims.get("exp")
    if type(expiry) is not int:
        raise PermissionError("the caller token has no expiry")
    return int(expiry - (time.time() if now is None else now))
