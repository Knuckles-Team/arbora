"""Doubles for scoped credential reuse: engine grants, a vault, a PKCE provider."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from arbora.access.credential_use import (
    AuthorizationChallenge,
    CredentialUseRequest,
    RefreshLocks,
)

NOW = 1_800_000_000.0
CODE = "code-" + "x" * 24
VERIFIER = "verifier-" + "y" * 24
TOKEN = "tok-" + "z" * 30
CALLER = SimpleNamespace(tenant="t1", principal="bob", client="cli-b", request_id="r1")


def request(**changes: Any) -> CredentialUseRequest:
    base = {
        "tenant": "t1",
        "owner_principal": "ada",
        "requesting_principal": "bob",
        "client": "cli-b",
        "connector": "mail-mcp",
        "provider": "idp",
        "audience": "https://api.example.test",
        "scopes": ("mail.read",),
        "resource": "mailbox:ada",
        "operation": "read",
    }
    base.update(changes)
    return CredentialUseRequest(**base)


class Ports:
    def __init__(self, *, profile: str = "jwt_oidc", granted: bool = True) -> None:
        self.profile = profile
        self.granted = granted
        self.vault: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, str]] = []
        self.clock_now = NOW
        self.backend_calls = 0
        self.refreshes = 0
        self.persist_fails = False
        self.callback: dict[str, Any] | None = None  # None: the user never answers
        self.interactive = True
        self.standing = "valid"
        self.decisions = 0
        self.refresh_locks = RefreshLocks()

    def clock(self) -> float:
        return self.clock_now

    def emit(self, event: Any) -> None:
        self.events.append(dict(event))

    async def decide(self, req: CredentialUseRequest) -> dict[str, Any]:
        self.decisions += 1
        return {
            "allow": self.granted,
            "decision_ref": "dec-1",
            "vault_ref": "vault:ada:idp",
            "expires_at": int(NOW) + 3600,
            "revocation_revision": 2,
            "token_profile": self.profile,
        }

    async def vault_get(self, ref: str) -> dict[str, Any] | None:
        return self.vault.get(ref)

    async def vault_put(self, ref: str, token: Any) -> str:
        if self.persist_fails:
            raise RuntimeError("vault down")
        self.vault[ref] = dict(token)
        return f"receipt:{ref}"

    async def validate(self, record: Any, grant: Any) -> str:
        return self.standing if record.get("access") == TOKEN else "expired"

    async def refresh(self, record: Any, grant: Any) -> dict[str, Any]:
        self.refreshes += 1
        await asyncio.sleep(0)
        return {"access": TOKEN, "refresh": "r2"}

    async def challenge(self, req: CredentialUseRequest, attempt: int) -> Any:
        if not self.interactive:
            return None
        return AuthorizationChallenge(
            request=req,
            issuer="https://idp.example.test",
            client_id="arbora",
            callback="http://127.0.0.1:8123/oauth/callback",
            state="state-123456",
            nonce="nonce-1",
            pkce_challenge="challenge-1",
            attempt=attempt,
            deadline=int(self.clock_now) + 120,
        )

    async def complete(self, challenge: Any, timeout: float) -> dict[str, Any]:
        if self.callback is None:
            await asyncio.sleep(timeout + 1)
        assert self.callback is not None
        return self.callback

    def use_backend(self, record: dict[str, Any]) -> None:
        self.backend_calls += 1
