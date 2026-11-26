"""SMTP send transport for identity email (ARBORA-IDENTITY-R016.2)."""

from __future__ import annotations

import asyncio

import pytest

from arbora.identity.mail_sender import (
    SmtpSettings,
    mint_link_token,
    send_identity_mail,
    token_hash,
)


class _Smtp:
    sessions: list[dict] = []

    def __init__(self, host, port, timeout):
        self.record = {"host": host, "port": port, "timeout": timeout, "steps": []}
        self.sessions.append(self.record)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context):
        self.record["steps"].append("starttls")

    def login(self, user, password):
        self.record["steps"].append(f"login:{user}")

    def send_message(self, message):
        self.record["steps"].append("send")
        self.record["to"] = message["To"]
        self.record["body"] = message.get_content()


SETTINGS = SmtpSettings(
    host="smtp.test",
    port=587,
    mail_from="id@example.test",
    username="u",
    password_ref="env://P",
)


@pytest.mark.spec("ARBORA-IDENTITY-R016.2")
def test_reset_mail_uses_starttls_and_audits_without_the_token() -> None:
    _Smtp.sessions.clear()
    events: list = []
    token = mint_link_token()
    assert len(token) >= 43 and token_hash(token) != token
    sent = asyncio.run(
        send_identity_mail(
            SETTINGS,
            "reset",
            "ada@example.test",
            f"https://arbora.test/auth/reset?token={token}",
            emit=events.append,
            resolve_password=lambda ref: "pw",
            smtp_factory=_Smtp,
        )
    )
    assert sent is True
    assert _Smtp.sessions[0]["steps"] == ["starttls", "login:u", "send"]
    assert _Smtp.sessions[0]["timeout"] == 10
    assert events[0]["kind"] == "reset" and "ada@example.test" not in repr(events)
    assert token not in repr(events)


@pytest.mark.spec("ARBORA-IDENTITY-R016.2")
def test_failure_is_uniform_and_never_raises() -> None:
    def broken(*args, **kwargs):
        raise OSError("relay down")

    events: list = []
    sent = asyncio.run(
        send_identity_mail(
            SETTINGS,
            "invite",
            "ada@example.test",
            "https://arbora.test/x",
            emit=events.append,
            resolve_password=lambda ref: "pw",
            smtp_factory=broken,
        )
    )
    assert sent is False and events[0]["outcome"] == "MAIL_SEND_FAILED"
