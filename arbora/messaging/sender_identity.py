"""Per-sender identity for inbound turns (ARBORA-MESSAGING-R007.1).

Each admitted ``<platform>:<user id>`` maps to one enrolled principal through
the config ``MESSAGING_SENDER_PRINCIPALS`` (a map from sender to principal id).
``session_for_sender`` obtains the turn session by RFC 8693 token exchange at
the identity provider: the subject is the sender's principal and the actor is
Arbora. The engine signer is the principal the exchanged token names. Either
typed refusal runs no turn and sends no reply.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import logging
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

from arbora.core.environment import read_env
from arbora.core.settings import RuntimeConfig

logger = logging.getLogger(__name__)


class SenderRefusal(enum.StrEnum):
    """The two typed refusals; either runs no turn and sends no reply."""

    UNMAPPED = "SENDER_UNMAPPED"
    EXCHANGE_REFUSED = "TOKEN_EXCHANGE_REFUSED"


SENDER_UNMAPPED = SenderRefusal.UNMAPPED
TOKEN_EXCHANGE_REFUSED = SenderRefusal.EXCHANGE_REFUSED
ACTING_AGENT = "agent:arbora"

Exchange = Callable[[str], str]


class SenderRefused(PermissionError):
    """A sender that cannot hold a turn session; ``code`` is one of the two refusals."""

    def __init__(self, code: SenderRefusal) -> None:
        self.code = code
        super().__init__(str(code))


@dataclasses.dataclass(frozen=True)
class SenderSession:
    """The turn session of one sender: its principal and the exchanged token."""

    principal: str
    token: str = dataclasses.field(repr=False)
    key: str


def _principal_map() -> Mapping[str, str]:
    raw: Any = read_env("MESSAGING_SENDER_PRINCIPALS")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raw = {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _exchange_for_principal(
    principal: str, config: RuntimeConfig, delegation: Mapping[str, Any]
) -> str:
    """Exchange Arbora's process token for one whose subject is ``principal``."""
    from arbora.security.delegated_auth import exchange_token_for_agent
    from arbora.security.request_identity import client_credentials_token
    from arbora.security.settings import security_settings

    return exchange_token_for_agent(
        ACTING_AGENT,
        subject_token=client_credentials_token(
            security_settings(config).kg_identity_oauth2
        ),
        requested_subject=principal,
        settings=config,
        config=delegation,
    )


def session_for_sender(
    platform: str,
    user_id: str,
    *,
    config: RuntimeConfig,
    delegation: Mapping[str, Any],
    exchange: Exchange | None = None,
) -> SenderSession:
    """Return the turn session of ``<platform>:<user_id>`` or raise ``SenderRefused``."""
    principal = _principal_map().get(f"{platform}:{user_id}", "")
    if not principal:
        raise SenderRefused(SENDER_UNMAPPED)
    try:
        token = (
            exchange
            or partial(_exchange_for_principal, config=config, delegation=delegation)
        )(principal)
    except Exception as exc:  # any exchange failure is one typed refusal
        logger.warning(
            "sender token exchange refused (%s)", type(exc).__name__, exc_info=True
        )
        raise SenderRefused(TOKEN_EXCHANGE_REFUSED) from None
    if not token:
        raise SenderRefused(TOKEN_EXCHANGE_REFUSED)
    return SenderSession(
        principal=principal,
        token=token,
        key=f"messaging:{platform}:{user_id}",
    )
