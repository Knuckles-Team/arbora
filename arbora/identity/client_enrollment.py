"""Machine client enrolment and the header-helper token (ARBORA-IDENTITY-R030.1, R030.2).

``arbora clients enroll`` creates one client-credentials client for one named client. It
grants only the scopes the operator names and stores the secret in the host secret store;
the secret reaches no terminal, log or plain file. ``arbora token`` prints one fresh access
token on standard output for a client header helper and caches it until shortly before
expiry. Every collaborator is an injected port.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from arbora.core.environment import read_env

__all__ = [
    "CACHE_SKEW_SECONDS",
    "ClientEnrollment",
    "ClientEnrollmentRefused",
    "ProviderAdmin",
    "enroll_client",
    "fetch_cached_token",
    "secret_path",
]

#: A cached token is reused until this many seconds before it expires.
CACHE_SKEW_SECONDS = 30
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")


class ClientEnrollmentRefused(Exception):
    """A stable code; nothing was changed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProviderAdmin(Protocol):
    """The identity provider admin interface of one realm."""

    async def client_exists(self, name: str) -> bool: ...

    async def create_client(self, name: str, scopes: Sequence[str]) -> str:
        """Create the client-credentials client and answer its secret."""


class SecretStore(Protocol):
    def set_if_absent(self, key: str, value: str, **metadata: Any) -> bool: ...


@dataclass(frozen=True, slots=True)
class ClientEnrollment:
    name: str
    scopes: tuple[str, ...]
    secret_ref: str


def secret_path(name: str) -> str:
    return f"arbora/clients/{name}/secret"


async def enroll_client(
    name: str,
    scopes: Sequence[str],
    *,
    provider: ProviderAdmin,
    secrets: SecretStore,
    audit: Callable[[Mapping[str, str]], None],
) -> ClientEnrollment:
    """Create one client and store its secret; answer the secret reference only."""
    if not _NAME.fullmatch(name or ""):
        raise ClientEnrollmentRefused("CLIENT_NAME_INVALID")
    granted = tuple(sorted(set(scopes)))
    if not granted or any(not _SCOPE.fullmatch(scope) for scope in granted):
        raise ClientEnrollmentRefused("CLIENT_SCOPES_INVALID")
    if await provider.client_exists(name):
        raise ClientEnrollmentRefused("CLIENT_EXISTS")
    secret = await provider.create_client(name, granted)
    ref = f"openbao://{secret_path(name)}"
    if not secrets.set_if_absent(secret_path(name), secret):
        raise ClientEnrollmentRefused("CLIENT_SECRET_STORE_FAILED")
    audit(
        {
            "op": "clients.enroll",
            "client": name,
            "scopes": " ".join(granted),
            "secret_ref": ref,
        }
    )
    return ClientEnrollment(name=name, scopes=granted, secret_ref=ref)


def _cache_file(client: str, directory: Path | None) -> Path:
    base = directory or Path(
        read_env("XDG_RUNTIME_DIR") or Path.home() / ".cache" / "arbora"
    )
    return base / f"token-{client}.json"


async def fetch_cached_token(
    client: str,
    *,
    request_token: Callable[[], Awaitable[Mapping[str, Any]]],
    clock: Callable[[], float] = time.time,
    cache_dir: Path | None = None,
) -> str:
    """One access token: the cached one until shortly before expiry, else a fresh one.

    ``request_token`` calls the token endpoint with the stored secret and answers
    ``{"access_token", "expires_in"}``. The cache file is private (0600) and holds only the
    access token and its expiry; the secret is never written.
    """
    path = _cache_file(client, cache_dir)
    now = clock()
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached["expires_at"] - CACHE_SKEW_SECONDS > now:
            return str(cached["access_token"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    answer = await request_token()
    token = str(answer["access_token"])
    expires_at = now + int(answer.get("expires_in", 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"access_token": token, "expires_at": expires_at}, handle)
    return token
