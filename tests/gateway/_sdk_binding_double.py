"""A structural double of scion-sdk ``main``'s ``credentials.binding`` module (PIN-LAG)."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import ModuleType

import pytest
from pydantic import BaseModel, ConfigDict, Field


class CredentialAccessBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tenant: str = Field(min_length=1)
    owner_principal: str = Field(min_length=1)
    requesting_principal: str = Field(min_length=1)
    client: str = Field(min_length=1)
    connector: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    scopes: tuple[str, ...] = ()
    resource: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    grant_ref: str = Field(min_length=1)
    vault_ref: str = Field(min_length=1)
    expires_at_ms: int = Field(ge=0)
    revocation_revision: str = Field(min_length=1)
    token_profile: str = Field(min_length=1)
    profile_revision: str = Field(min_length=1)


_current: ContextVar[CredentialAccessBinding | None] = ContextVar(
    "double_binding", default=None
)


def binding_digest(binding: CredentialAccessBinding) -> str:
    canonical = json.dumps(
        binding.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def current_credential_binding() -> CredentialAccessBinding | None:
    return _current.get()


@contextmanager
def use_credential_binding(binding: CredentialAccessBinding | None) -> Iterator[None]:
    token = _current.set(binding)
    try:
        yield
    finally:
        _current.reset(token)


def install(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = ModuleType("scion_sdk.credentials.binding")
    for fn in (
        CredentialAccessBinding,
        binding_digest,
        current_credential_binding,
        use_credential_binding,
    ):
        setattr(module, fn.__name__, fn)
    monkeypatch.setitem(sys.modules, "scion_sdk.credentials.binding", module)
    return module


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
