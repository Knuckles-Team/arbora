"""Graph OS outbound HTTP clients and TLS profile resolution.

Every outbound client Graph OS builds comes from this module. A client sends a
Graph OS ``User-Agent``, ignores proxy variables from the environment, refuses
disabled certificate verification and an unbounded timeout, and, when asked,
checks each destination against the SDK egress policy before it sends.
TLS profiles resolve through :func:`agent_connector_sdk.tls.resolve_tls_profile`.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from agent_connector_sdk.http.egress import EgressOptions, check_egress_destination
from agent_connector_sdk.tls.resolve import (
    resolve_tls_profile as _sdk_resolve_tls_profile,
)

__all__ = [
    "advertised_self_hosts",
    "is_self_url",
    "DEFAULT_HTTP_TIMEOUT_S",
    "create_async_http_client",
    "create_http_client",
    "is_loopback_url",
    "resolve_configured_tls_profile",
    "resolve_tls_profile",
    "standard_headers",
    "tls_environment_from_config",
]

DEFAULT_HTTP_TIMEOUT_S = 30.0
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@lru_cache(maxsize=1)
def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("graph-os")
    except Exception:  # noqa: BLE001 - a version lookup never breaks a client
        return "0"


def standard_headers() -> dict[str, str]:
    """The headers every outbound Graph OS call carries."""
    return {"User-Agent": f"graph-os/{_package_version()}"}


def is_loopback_url(url: str) -> bool:
    """True when ``url`` names a loopback host."""
    host = (urlsplit(str(url)).hostname or "").lower()
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def advertised_self_hosts() -> set[str]:
    """The named hosts that address this process: ``MCP_ALLOWED_HOSTS`` and a concrete bind ``HOST``."""
    from graph_os.core.settings import setting

    hosts = {
        token.strip().lower()
        for token in str(setting("MCP_ALLOWED_HOSTS", "") or "")
        .replace(",", " ")
        .split()
        if token.strip()
    }
    bind = str(setting("HOST", "") or "").strip().lower()
    if bind and bind not in {"0.0.0.0", "::", "[::]", "*"}:  # nosec B104 - wildcard names, not a bind
        hosts.add(bind)
    return hosts - _LOOPBACK_HOSTS


def is_self_url(url: str) -> bool:
    """True when ``url`` names one of this process's advertised hosts, on any port."""
    host = (urlsplit(str(url or "")).hostname or "").lower()
    return bool(host) and host in advertised_self_hosts()


def _client_kwargs(
    timeout: Any, verify: Any, headers: Mapping[str, str] | None, extra: dict[str, Any]
) -> dict[str, Any]:
    if timeout is None:
        raise ValueError("an outbound client needs a finite timeout")
    if verify is False:
        raise ValueError("an outbound client never disables certificate verification")
    merged = {**standard_headers(), **dict(headers or {})}
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "headers": merged,
        "trust_env": False,
        **extra,
    }
    if "transport" not in kwargs:
        kwargs["verify"] = verify
    return kwargs


def _egress(
    pin_egress: bool, allowed_private_hosts: Collection[str], allow_loopback: bool
) -> EgressOptions | None:
    if not pin_egress:
        return None
    return EgressOptions(
        pin_egress=True,
        allowed_private_hosts=tuple(allowed_private_hosts),
        allow_loopback=allow_loopback,
    )


class _CheckedTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport, options: EgressOptions) -> None:
        self._inner = inner
        self._options = options

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        check_egress_destination(request.url.host, self._options)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


class _AsyncCheckedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, options: EgressOptions) -> None:
        self._inner = inner
        self._options = options

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        check_egress_destination(request.url.host, self._options)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def create_http_client(
    *,
    timeout: float | httpx.Timeout = DEFAULT_HTTP_TIMEOUT_S,
    verify: Any = True,
    headers: Mapping[str, str] | None = None,
    pin_egress: bool = False,
    allowed_private_hosts: Collection[str] = (),
    allow_loopback: bool = False,
    **httpx_kwargs: Any,
) -> httpx.Client:
    """A synchronous outbound client (see the module docstring)."""
    kwargs = _client_kwargs(timeout, verify, headers, httpx_kwargs)
    options = _egress(pin_egress, allowed_private_hosts, allow_loopback)
    if options is not None:
        inner = kwargs.pop("transport", None) or httpx.HTTPTransport(verify=verify)
        kwargs["transport"] = _CheckedTransport(inner, options)
    return httpx.Client(**kwargs)


def create_async_http_client(
    *,
    timeout: float | httpx.Timeout = DEFAULT_HTTP_TIMEOUT_S,
    verify: Any = True,
    headers: Mapping[str, str] | None = None,
    pin_egress: bool = False,
    allowed_private_hosts: Collection[str] = (),
    allow_loopback: bool = False,
    **httpx_kwargs: Any,
) -> httpx.AsyncClient:
    """An asynchronous outbound client; see :func:`create_http_client`."""
    kwargs = _client_kwargs(timeout, verify, headers, httpx_kwargs)
    options = _egress(pin_egress, allowed_private_hosts, allow_loopback)
    if options is not None:
        inner = kwargs.pop("transport", None) or httpx.AsyncHTTPTransport(verify=verify)
        kwargs["transport"] = _AsyncCheckedTransport(inner, options)
    return httpx.AsyncClient(**kwargs)


def resolve_tls_profile(
    service: str,
    *,
    profile_name: str | None = None,
    profile_ref: str | None = None,
    profile: Mapping[str, Any] | None = None,
    destination_root: Path | None = None,
    **_unused: Any,
) -> Any:
    """Resolve ``service``'s TLS profile through the SDK resolver."""
    return _sdk_resolve_tls_profile(
        service,
        profile_name=profile_name or None,
        profile_ref=profile_ref or None,
        profile=profile,
        runtime_root=destination_root,
    )


def resolve_configured_tls_profile(
    service: str,
    *,
    profile_name: str | None = None,
    profile_ref: str | None = None,
    profile: Mapping[str, Any] | None = None,
    destination_root: Path | None = None,
    **_unused: Any,
) -> Any:
    """Resolve ``service``'s TLS profile from the process settings."""
    return resolve_tls_profile(
        service,
        profile_name=profile_name,
        profile_ref=profile_ref,
        profile=profile,
        destination_root=destination_root,
    )


def tls_environment_from_config(
    config: Any = None, *, base_environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The TLS selectors a resolver reads: the process environment."""
    del config
    return dict(os.environ if base_environ is None else base_environ)
