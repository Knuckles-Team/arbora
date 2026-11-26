"""SMTP send transport for identity email (ARBORA-IDENTITY-R016.2).

One transport only: SMTP through ``smtplib.SMTP`` with STARTTLS. A relay such as a
transactional mail service is configured as the SMTP host; there is no separate connector
call. The send runs in a worker thread with a 10 s timeout. A failure is logged as
``MAIL_SEND_FAILED`` and the public reply stays uniform, so the function never raises. The
link token is minted here (32 URL-safe bytes); the engine stores only its hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import smtplib
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

__all__ = [
    "SEND_TIMEOUT_SECONDS",
    "SmtpSettings",
    "mint_link_token",
    "send_identity_mail",
    "token_hash",
]

SEND_TIMEOUT_SECONDS = 10
KINDS = ("reset", "verify", "invite")
_SUBJECTS = {
    "reset": "Reset your password",
    "verify": "Verify your email address",
    "invite": "You are invited",
}
_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    """``identity.smtp.{host, port, mail_from, username, password_ref, starttls}``."""

    host: str
    port: int
    mail_from: str
    username: str = ""
    password_ref: str = ""
    starttls: bool = True

    def __post_init__(self) -> None:
        if not self.host or not 1 <= self.port <= 65535 or "@" not in self.mail_from:
            raise ValueError("SMTP settings are invalid")

    @classmethod
    def from_config(cls, document: Mapping[str, Any]) -> SmtpSettings:
        block = (document.get("identity") or {}).get("smtp") or {}
        return cls(
            host=str(block.get("host", "")),
            port=int(block.get("port", 587)),
            mail_from=str(block.get("mail_from", "")),
            username=str(block.get("username", "")),
            password_ref=str(block.get("password_ref", "")),
            starttls=bool(block.get("starttls", True)),
        )


def mint_link_token() -> str:
    """A 32-byte URL-safe token, usable once; only :func:`token_hash` is stored."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _resolve_password(ref: str) -> str:
    if not ref:
        return ""
    from arbora.security import client_credentials as credentials

    if ref.startswith("env://"):
        return credentials._resolve_env_secret(ref.removeprefix("env://"))
    return credentials._resolve_vault_secret(ref)


def _deliver(
    settings: SmtpSettings,
    message: EmailMessage,
    password: str,
    smtp_factory: Callable[..., Any],
) -> None:
    with smtp_factory(
        settings.host, settings.port, timeout=SEND_TIMEOUT_SECONDS
    ) as client:
        if settings.starttls:
            client.starttls(context=ssl.create_default_context())
        if settings.username:
            client.login(settings.username, password)
        client.send_message(message)


async def send_identity_mail(
    settings: SmtpSettings,
    kind: str,
    to: str,
    link: str,
    *,
    emit: Callable[[Mapping[str, str]], None] = lambda event: None,
    resolve_password: Callable[[str], str] = _resolve_password,
    smtp_factory: Callable[..., Any] = smtplib.SMTP,
) -> bool:
    """Send one identity mail; answer whether it was handed to the relay, never raise."""
    recipient = hashlib.sha256(to.encode()).hexdigest()[:16]
    outcome = "sent"
    try:
        if kind not in KINDS or "@" not in to or "\n" in to + link:
            raise ValueError("mail request is invalid")
        message = EmailMessage()
        message["From"] = settings.mail_from
        message["To"] = to
        message["Subject"] = _SUBJECTS[kind]
        message.set_content(f"{_SUBJECTS[kind]}: {link}")
        password = await asyncio.to_thread(resolve_password, settings.password_ref)
        await asyncio.wait_for(
            asyncio.to_thread(_deliver, settings, message, password, smtp_factory),
            timeout=SEND_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - the public reply stays uniform
        outcome = "MAIL_SEND_FAILED"
        _log.warning("MAIL_SEND_FAILED kind=%s recipient=%s", kind, recipient)
    emit(
        {
            "op": "identity.mail",
            "kind": kind,
            "recipient": recipient,
            "outcome": outcome,
        }
    )
    return outcome == "sent"
