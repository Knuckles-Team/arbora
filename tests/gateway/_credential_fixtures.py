"""Fixtures for the credential authority tests: the engine decision and the connector profile."""

from __future__ import annotations


def decision(**over: object) -> dict[str, object]:
    """The engine's ``ResourceAccessDecision`` of an allowed, current grant."""
    base: dict[str, object] = {
        "allow": True,
        "grant_id": "grant:1",
        "scope_ceiling": ["read", "send"],
        "expires_at_ms": 5_000,
        "revocation_revision": 1,
        "reason_code": None,
    }
    return {**base, **over}


PROFILE: dict[str, object] = {
    "resource": "mailbox:1",
    "protocol": "mcp",
    "scopes": ["read"],
    "provider": "mail",
    "audience": "https://mail.example",
    "vault_ref": "openbao://apps/mail-agent#TOKEN",
    "token_profile": "bearer",
    "profile_revision": "p1",
}
