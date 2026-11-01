"""Outbound HTTP clients and secret references for channel adapters.

Every channel adapter builds its HTTP client here, from the service's TLS
profile, and resolves its credential references here. Peer and hostname
verification stay on; redirects and ambient proxy settings stay off.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from scion_sdk.credentials.references import SecretReferenceError
from scion_sdk.credentials.resolution import resolve_secret_reference
from scion_sdk.credentials.resolver import CredentialUnavailableError
from scion_sdk.tls.resolve import resolve_tls_profile

__all__ = ["channel_http_client", "resolve_secret"]

logger = logging.getLogger(__name__)


def channel_http_client(service: str, **kwargs: Any) -> httpx.AsyncClient:
    """Return an async client that trusts the TLS profile of ``service``."""
    trust = resolve_tls_profile(service)
    try:
        options = {**trust.httpx_kwargs(), "follow_redirects": False, **kwargs}
        return httpx.AsyncClient(**options)
    finally:
        trust.cleanup()


def resolve_secret(reference: str) -> str:
    """Resolve a secret reference to its value, or ``""`` when unresolved."""
    try:
        return str(resolve_secret_reference(reference) or "").strip()
    except (SecretReferenceError, CredentialUnavailableError) as exc:
        logger.warning("secret reference unresolved (%s)", type(exc).__name__)
        return ""
