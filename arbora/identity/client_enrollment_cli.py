"""The ``arbora clients enroll`` and ``arbora token`` commands (ARBORA-IDENTITY-R030.1, R030.2).

Standard output carries the secret reference (enrol) or one access token (token) and nothing
else. Standard error carries a stable code on refusal and never a secret.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets as _secrets
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from arbora.core.environment import read_env
from arbora.identity.client_enrollment import (
    ClientEnrollmentRefused,
    ProviderAdmin,
    enroll_client,
    fetch_cached_token,
)

__all__ = ["KeycloakProviderAdmin", "enroll_main", "token_main"]


class KeycloakProviderAdmin:
    """The provider admin interface over the realm admin caller of deployment provisioning."""

    def __init__(self, admin: Callable[..., Any]) -> None:
        self._admin = admin

    async def client_exists(self, name: str) -> bool:
        found = await asyncio.to_thread(self._admin, f"/clients?clientId={name}")
        return bool(found)

    async def create_client(self, name: str, scopes: Sequence[str]) -> str:
        secret = _secrets.token_urlsafe(32)
        await asyncio.to_thread(
            self._admin,
            "/clients",
            "POST",
            {
                "clientId": name,
                "enabled": True,
                "publicClient": False,
                "serviceAccountsEnabled": True,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "secret": secret,
                "defaultClientScopes": list(scopes),
                "optionalClientScopes": [],
            },
        )
        return secret


def _audit(event: Mapping[str, str]) -> None:
    import logging

    logging.getLogger("arbora.identity.clients").info(
        "clients.enroll", extra={"audit": dict(event)}
    )


def refuse_admin_for_config(path: Any) -> Any:
    """The default ``admin_for_config``: no realm admin is composed, so the command fails closed."""
    raise ValueError("no realm admin is composed for this config")


def _composed_ports(
    config_path: str | None,
    provider: Any,
    secrets: Any,
    admin_for_config: Callable[[Any], Any],
) -> tuple[Any, Any]:
    """Compose the Keycloak adapter and the secret store from the console config.

    The config is the ``deploy.yml`` of the realm (``--config`` or ``ARBORA_DEPLOY_CONFIG``).
    An absent or unreadable config leaves the port unset, so the command fails closed.
    """
    from pathlib import Path

    path = config_path or read_env("ARBORA_DEPLOY_CONFIG")
    if not path:
        return provider, secrets
    try:
        if provider is None:
            provider = KeycloakProviderAdmin(admin_for_config(Path(path)))
        if secrets is None:
            from arbora.security.secrets import create_secrets_client

            secrets = create_secrets_client()
    except Exception:
        logging.getLogger(__name__).debug(
            "an unreadable config fails closed with one code", exc_info=True
        )
        return None, None
    return provider, secrets


def enroll_main(
    argv: Sequence[str],
    *,
    provider: ProviderAdmin | None = None,
    secrets: Any = None,
    admin_for_config: Callable[[Any], Any] = refuse_admin_for_config,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    out, err = stdout or sys.stdout, stderr or sys.stderr
    parser = argparse.ArgumentParser(prog="arbora clients enroll")
    parser.add_argument("name")
    parser.add_argument("--scope", action="append", default=[], dest="scopes")
    parser.add_argument("--config", default=None, help="the deploy.yml of the realm")
    args = parser.parse_args(list(argv))
    provider, secrets = _composed_ports(
        args.config, provider, secrets, admin_for_config
    )
    if provider is None or secrets is None:
        print("PROVIDER_ADMIN_UNAVAILABLE", file=err)
        return 2
    try:
        enrollment = asyncio.run(
            enroll_client(
                args.name,
                args.scopes,
                provider=provider,
                secrets=secrets,
                audit=_audit,
            )
        )
    except ClientEnrollmentRefused as refused:
        print(refused.code, file=err)
        return 2
    print(enrollment.secret_ref, file=out)
    return 0


def _provider_token_request(
    client: str,
) -> Callable[[], Awaitable[Mapping[str, Any]]] | None:
    """The client-credentials request of one enrolled client; the secret stays in memory."""
    from arbora.identity.client_enrollment import secret_path
    from arbora.security import client_credentials as credentials

    url = credentials._derive_token_url()
    if not url:
        return None

    async def request() -> Mapping[str, Any]:
        import httpx

        secret = credentials._resolve_vault_secret(f"openbao://{secret_path(client)}")
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.post(
                url,
                data={"grant_type": "client_credentials"},
                auth=(client, secret),
            )
            response.raise_for_status()
            return dict(response.json())

    return request


def token_main(
    argv: Sequence[str],
    *,
    request_token: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
    clock: Callable[[], float] | None = None,
    cache_dir: Any = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    import time

    out, err = stdout or sys.stdout, stderr or sys.stderr
    parser = argparse.ArgumentParser(prog="arbora token")
    parser.add_argument("--client", required=True)
    args = parser.parse_args(list(argv))
    if request_token is None:
        request_token = _provider_token_request(args.client)
    if request_token is None:
        print("TOKEN_ENDPOINT_UNAVAILABLE", file=err)
        return 2
    try:
        token = asyncio.run(
            fetch_cached_token(
                args.client,
                request_token=request_token,
                clock=clock or time.time,
                cache_dir=cache_dir,
            )
        )
    except Exception:
        logging.getLogger(__name__).debug(
            "a stable code, never the cause text", exc_info=True
        )
        print("TOKEN_FETCH_FAILED", file=err)
        return 2
    print(token, file=out)
    return 0
