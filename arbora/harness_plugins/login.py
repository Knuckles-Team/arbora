"""The hook runner credential (ARBORA-HARNESS-PLUGINS-R003.2, R003.3).

A harness whose hooks run commands has no access to the harness's own token
store, so ``arbora plugin login`` gives the runner its own refresh token: an
authorization code flow with PKCE on the profile's fixed loopback callback,
with the native public client. The refresh token lives in the platform
credential store when one is importable, else in a mode-0600 file under
``<arbora config>/credentials/``. The access token is renewed before expiry.

The credential has one explicit machine (:data:`CREDENTIAL_EDGES`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from arbora.harness_plugins.compose import NATIVE_CLIENT_ID
from arbora.harness_plugins.profiles import HarnessProfile

KEYRING_SERVICE = "arbora-harness-plugin"
#: Renew when the access token has less than this many seconds left.
RENEW_MARGIN_S = 60


class CredentialState(StrEnum):
    UNAUTHENTICATED = "unauthenticated"
    AUTHORIZING = "authorizing"
    AUTHORIZED = "authorized"
    REFRESHING = "refreshing"
    EXPIRED = "expired"
    REVOKED = "revoked"


class CredentialEvent(StrEnum):
    BEGIN = "begin"
    GRANT = "grant"
    DENY = "deny"
    REFRESH_DUE = "refresh_due"
    REFRESH_OK = "refresh_ok"
    REFRESH_FAIL = "refresh_fail"
    REVOKE = "revoke"


S, E = CredentialState, CredentialEvent
CREDENTIAL_EDGES: dict[tuple[CredentialState, CredentialEvent], CredentialState] = {
    (S.UNAUTHENTICATED, E.BEGIN): S.AUTHORIZING,
    (S.AUTHORIZING, E.GRANT): S.AUTHORIZED,
    (S.AUTHORIZING, E.DENY): S.UNAUTHENTICATED,
    (S.AUTHORIZED, E.REFRESH_DUE): S.REFRESHING,
    (S.REFRESHING, E.REFRESH_OK): S.AUTHORIZED,
    (S.REFRESHING, E.REFRESH_FAIL): S.EXPIRED,
    (S.AUTHORIZED, E.REVOKE): S.REVOKED,
    (S.EXPIRED, E.BEGIN): S.AUTHORIZING,
    (S.REVOKED, E.BEGIN): S.AUTHORIZING,
}


class IllegalCredentialTransition(RuntimeError):
    code = "CREDENTIAL_TRANSITION_ILLEGAL"

    def __init__(self, state: CredentialState, event: CredentialEvent) -> None:
        super().__init__(f"{self.code}: {state} --{event}-->")
        self.state = state
        self.event = event


def transition(state: CredentialState, event: CredentialEvent) -> CredentialState:
    target = CREDENTIAL_EDGES.get((state, event))
    if target is None:
        raise IllegalCredentialTransition(state, event)
    return target


@dataclass
class Credential:
    harness: str
    base_url: str
    token_endpoint: str
    refresh_token: str
    access_token: str = ""
    expires_at: float = 0.0
    state: CredentialState = CredentialState.AUTHORIZED

    def to_json(self) -> str:
        data = dict(self.__dict__)
        data["state"] = self.state.value
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> Credential:
        data = json.loads(raw)
        data["state"] = CredentialState(data.get("state", "authorized"))
        return cls(**data)


class TokenStore:
    """The platform credential store when importable, else a 0600 file."""

    def __init__(self, directory: Path | None = None) -> None:
        if directory is None:
            from arbora.core.settings import config_dir

            directory = config_dir() / "credentials"
        self.directory = directory

    @staticmethod
    def _keyring() -> Any:
        if os.environ.get("ARBORA_PLUGIN_FILE_STORE") == "1":
            return None
        try:
            import keyring
        except ImportError:
            return None
        return keyring

    def _path(self, harness: str) -> Path:
        return self.directory / f"{harness}.json"

    def save(self, credential: Credential) -> None:
        ring = self._keyring()
        if ring is not None:
            ring.set_password(KEYRING_SERVICE, credential.harness, credential.to_json())
            return
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(credential.harness)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(credential.to_json())
        os.chmod(path, 0o600)

    def load(self, harness: str) -> Credential | None:
        ring = self._keyring()
        raw = ring.get_password(KEYRING_SERVICE, harness) if ring is not None else None
        if raw is None and self._path(harness).is_file():
            raw = self._path(harness).read_text(encoding="utf-8")
        return Credential.from_json(raw) if raw else None

    def delete(self, harness: str) -> None:
        ring = self._keyring()
        if ring is not None:
            try:
                ring.delete_password(KEYRING_SERVICE, harness)
            except Exception:  # noqa: BLE001 - an absent entry is already deleted
                return
        self._path(harness).unlink(missing_ok=True)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


def discover(base_url: str, client: httpx.Client) -> Mapping[str, Any]:
    """Authorization server metadata through the door's protected-resource document."""
    resource = client.get(
        f"{base_url.rstrip('/')}/.well-known/oauth-protected-resource/mcp"
    )
    resource.raise_for_status()
    issuer = str(resource.json()["authorization_servers"][0]).rstrip("/")
    metadata = client.get(f"{issuer}/.well-known/openid-configuration")
    metadata.raise_for_status()
    return metadata.json()


def _wait_for_code(callback: str, expected_state: str, timeout_s: float) -> str:
    parts = urlsplit(callback)
    received: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server interface
            query = parse_qs(urlsplit(self.path).query)
            if urlsplit(self.path).path == parts.path:
                received.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"arbora: sign-in complete, you can close this tab.")

        def log_message(self, *_args: Any) -> None:
            return

    server = HTTPServer((parts.hostname or "127.0.0.1", parts.port or 80), Handler)
    server.timeout = timeout_s
    deadline = time.monotonic() + timeout_s
    while (
        "code" not in received
        and "error" not in received
        and time.monotonic() < deadline
    ):
        server.handle_request()
    server.server_close()
    if received.get("state") != expected_state or "code" not in received:
        raise PermissionError(received.get("error", "authorization was not granted"))
    return received["code"]


def authorize(
    profile: HarnessProfile,
    base_url: str,
    *,
    store: TokenStore,
    open_url: Callable[[str], Any],
    client: httpx.Client | None = None,
    wait_for_code: Callable[[str, str, float], str] = _wait_for_code,
    timeout_s: float = 300.0,
) -> Credential:
    """Run the PKCE flow on the profile callback and store the refresh token."""
    state = transition(CredentialState.UNAUTHENTICATED, CredentialEvent.BEGIN)
    http = client or httpx.Client(timeout=30)
    metadata = discover(base_url, http)
    verifier, challenge = pkce_pair()
    nonce = secrets.token_urlsafe(16)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": NATIVE_CLIENT_ID,
            "redirect_uri": profile.callback,
            "scope": "openid offline_access",
            "state": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    open_url(f"{metadata['authorization_endpoint']}?{query}")
    try:
        code = wait_for_code(profile.callback, nonce, timeout_s)
    except PermissionError:
        transition(state, CredentialEvent.DENY)
        raise
    response = http.post(
        str(metadata["token_endpoint"]),
        data={
            "grant_type": "authorization_code",
            "client_id": NATIVE_CLIENT_ID,
            "code": code,
            "redirect_uri": profile.callback,
            "code_verifier": verifier,
        },
    )
    response.raise_for_status()
    tokens = response.json()
    credential = Credential(
        harness=profile.id,
        base_url=base_url,
        token_endpoint=str(metadata["token_endpoint"]),
        refresh_token=str(tokens["refresh_token"]),
        access_token=str(tokens.get("access_token", "")),
        expires_at=time.time() + float(tokens.get("expires_in", 0)),
        state=transition(state, CredentialEvent.GRANT),
    )
    store.save(credential)
    return credential


def renew(
    credential: Credential, *, store: TokenStore, client: httpx.Client | None = None
) -> Credential:
    """A fresh access token when the current one is near expiry."""
    if credential.access_token and credential.expires_at - time.time() > RENEW_MARGIN_S:
        return credential
    credential.state = transition(credential.state, CredentialEvent.REFRESH_DUE)
    http = client or httpx.Client(timeout=10)
    response = http.post(
        credential.token_endpoint,
        data={
            "grant_type": "refresh_token",
            "client_id": NATIVE_CLIENT_ID,
            "refresh_token": credential.refresh_token,
        },
    )
    if response.status_code != 200:
        credential.state = transition(credential.state, CredentialEvent.REFRESH_FAIL)
        store.save(credential)
        return credential
    tokens = response.json()
    credential.access_token = str(tokens["access_token"])
    credential.refresh_token = str(
        tokens.get("refresh_token", credential.refresh_token)
    )
    credential.expires_at = time.time() + float(tokens.get("expires_in", 0))
    credential.state = transition(credential.state, CredentialEvent.REFRESH_OK)
    store.save(credential)
    return credential


__all__ = [
    "CREDENTIAL_EDGES",
    "Credential",
    "CredentialEvent",
    "CredentialState",
    "IllegalCredentialTransition",
    "TokenStore",
    "authorize",
    "pkce_pair",
    "renew",
    "transition",
]
