"""The MCP server's command-line parser, its secret defaults and the network exposure validation (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
from typing import Any

from scion_sdk.credentials.runtime_references import (
    RuntimeSecretReferenceAction,
    RuntimeSecretReferenceError,
    resolve_runtime_secret_reference,
)
from scion_sdk.utilities import to_boolean

from arbora.core.environment import read_env
from arbora.mcp_server.backing.server_filters import (
    _ALL_TRANSPORTS,
    _NETWORK_TRANSPORTS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TRANSPORT,
    _split_csv,
)

logger = logging.getLogger(__name__)


def _is_loopback_bind(host: Any) -> bool:
    """Return true only for an explicit loopback bind target.

    Hostnames other than ``localhost`` are deliberately treated as remote. We
    do not resolve DNS while validating startup configuration because a name
    can be rebound after validation.
    """
    value = str(host or "").strip().lower()
    if value in {"localhost", "localhost."}:
        return True
    value = value.strip("[]").split("%", 1)[0]
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _requires_network_exposure_validation(args: argparse.Namespace) -> bool:
    transport = str(getattr(args, "transport", "stdio") or "stdio").lower()
    if transport not in _NETWORK_TRANSPORTS:
        return False
    return not _is_loopback_bind(getattr(args, "host", ""))


def _validate_network_auth_present(args: argparse.Namespace) -> None:
    auth_type = str(getattr(args, "auth_type", "none") or "none").lower()
    if auth_type == "none":
        logger.error(
            "Refusing an unauthenticated MCP network listener outside loopback"
        )
        raise SystemExit(1)


def _validate_network_tls_material(args: argparse.Namespace) -> bool:
    """Validate cert/key policy; returns whether direct TLS is configured."""
    certfile = str(getattr(args, "tls_certfile", "") or "").strip()
    keyfile = str(getattr(args, "tls_keyfile", "") or "").strip()
    if bool(certfile) != bool(keyfile):
        logger.error("MCP server TLS requires both certificate and key material")
        raise SystemExit(1)
    direct_tls = bool(certfile and keyfile)
    if direct_tls and not (os.path.isfile(certfile) and os.path.isfile(keyfile)):
        logger.error("MCP server TLS material is unavailable")
        raise SystemExit(1)
    return direct_tls


def _validate_network_tls_boundary(args: argparse.Namespace, direct_tls: bool) -> None:
    terminated = bool(getattr(args, "tls_terminated", False))
    proxy_cidrs = str(getattr(args, "trusted_proxy_cidrs", "") or "").strip()
    if terminated and not proxy_cidrs:
        logger.error(
            "MCP_TLS_TERMINATED requires MCP_TRUSTED_PROXY_CIDRS so plaintext "
            "traffic is accepted only from the TLS ingress"
        )
        raise SystemExit(1)
    if not direct_tls and not terminated:
        logger.error(
            "A non-loopback MCP listener requires direct TLS or an explicitly "
            "trusted TLS-terminating ingress"
        )
        raise SystemExit(1)


def _validate_network_allowed_hosts(args: argparse.Namespace) -> None:
    hosts = _split_csv(str(getattr(args, "allowed_hosts", "") or ""))
    if not hosts or any("*" in value for value in hosts):
        logger.error("A non-loopback MCP listener requires exact MCP_ALLOWED_HOSTS")
        raise SystemExit(1)


def _validate_network_exposure(args: argparse.Namespace) -> None:
    """Fail closed unless a remote MCP listener has auth and a TLS boundary."""
    if not _requires_network_exposure_validation(args):
        return
    _validate_network_auth_present(args)
    direct_tls = _validate_network_tls_material(args)
    _validate_network_tls_boundary(args, direct_tls)
    _validate_network_allowed_hosts(args)


def _resolve_mcp_parser_secret_defaults() -> tuple[
    str | None, str | None, str | None, str | None, str | None, str | None
]:
    """(default_oidc_config, default_oidc_client_id, default_oidc_client_secret,
    default_oauth_upstream_client_secret, default_openapi_password,
    default_openapi_client_secret) -- resolved from Keycloak/env-var defaults
    and runtime-secret-ref lookups, for create_mcp_parser's flag defaults."""
    keycloak_url = read_env("KEYCLOAK_URL")
    keycloak_realm = read_env("KEYCLOAK_REALM")
    default_oidc_config = read_env("OIDC_CONFIG_URL")
    if not default_oidc_config and keycloak_url:
        default_oidc_config = (
            f"{keycloak_url}/realms/{keycloak_realm}/.well-known/openid-configuration"
        )

    default_oidc_client_id = read_env("OIDC_CLIENT_ID") or read_env(
        "KEYCLOAK_CLIENT_ID"
    )
    default_oidc_client_secret_ref = read_env("OIDC_CLIENT_SECRET_REF") or read_env(
        "KEYCLOAK_CLIENT_SECRET_REF"
    )
    try:
        default_oidc_client_secret = (
            resolve_runtime_secret_reference(default_oidc_client_secret_ref)
            if default_oidc_client_secret_ref
            else None
        )
        oauth_upstream_client_secret_ref = read_env("OAUTH_UPSTREAM_CLIENT_SECRET_REF")
        default_oauth_upstream_client_secret = (
            resolve_runtime_secret_reference(oauth_upstream_client_secret_ref)
            if oauth_upstream_client_secret_ref
            else None
        )
        openapi_password_ref = read_env("OPENAPI_PASSWORD_REF")
        default_openapi_password = (
            resolve_runtime_secret_reference(openapi_password_ref)
            if openapi_password_ref
            else None
        )
        openapi_client_secret_ref = read_env("OPENAPI_CLIENT_SECRET_REF")
        default_openapi_client_secret = (
            resolve_runtime_secret_reference(openapi_client_secret_ref)
            if openapi_client_secret_ref
            else None
        )
    except RuntimeSecretReferenceError as exc:
        raise RuntimeError("configured authentication secret is unavailable") from exc

    return (
        default_oidc_config,
        default_oidc_client_id,
        default_oidc_client_secret,
        default_oauth_upstream_client_secret,
        default_openapi_password,
        default_openapi_client_secret,
    )


def _validate_transport_choices(transport_choices: tuple[str, ...]) -> None:
    if not transport_choices or any(
        transport not in _ALL_TRANSPORTS for transport in transport_choices
    ):
        raise ValueError(
            "transport_choices must be a non-empty subset of supported transports"
        )


def create_mcp_parser(
    *,
    transport_choices: tuple[str, ...] = _ALL_TRANSPORTS,
    resolve_secret_defaults: bool = True,
) -> argparse.ArgumentParser:
    """Create a standard argument parser for MCP servers.

    Defines a comprehensive set of CLI flags for transport selection,
    host/port configuration, authentication (JWT, OIDC, OAuth), and Eunomia
    policy enforcement. Callers may narrow ``transport_choices`` when a server
    intentionally exposes a smaller current transport surface.
    ``resolve_secret_defaults=False`` leaves the secret flag defaults unset so
    usage text never reads configuration or a secret store.

    Returns:
        An argparse.ArgumentParser instance.

    """
    (
        default_oidc_config,
        default_oidc_client_id,
        default_oidc_client_secret,
        default_oauth_upstream_client_secret,
        default_openapi_password,
        default_openapi_client_secret,
    ) = (
        _resolve_mcp_parser_secret_defaults()
        if resolve_secret_defaults
        else (None, None, None, None, None, None)
    )
    _validate_transport_choices(transport_choices)

    parser = argparse.ArgumentParser(add_help=False, description="MCP Server")
    parser.add_argument(
        "-t",
        "--transport",
        default=DEFAULT_TRANSPORT,
        choices=list(transport_choices),
        help=f"Transport method: {', '.join(transport_choices)} (default: {DEFAULT_TRANSPORT})",
    )
    parser.add_argument(
        "-H",
        "--host",
        default=DEFAULT_HOST,
        help="Host address for HTTP transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Port number for HTTP transport (default: 8000)",
    )
    parser.add_argument(
        "--tls-certfile",
        default=read_env("MCP_TLS_CERTFILE"),
        help="Runtime path to the inbound TLS certificate (prefer secret projection)",
    )
    parser.add_argument(
        "--tls-keyfile",
        default=read_env("MCP_TLS_KEYFILE"),
        help="Runtime path to the inbound TLS private key (prefer secret projection)",
    )
    parser.add_argument(
        "--tls-terminated",
        action="store_true",
        default=to_boolean(read_env("MCP_TLS_TERMINATED")),
        help="TLS is terminated by a peer restricted with --trusted-proxy-cidrs",
    )
    parser.add_argument(
        "--trusted-proxy-cidrs",
        default=read_env("MCP_TRUSTED_PROXY_CIDRS"),
        help="Comma-separated exact ingress peer CIDRs used with --tls-terminated",
    )
    parser.add_argument(
        "--allowed-hosts",
        default=read_env("MCP_ALLOWED_HOSTS"),
        help="Comma-separated exact Host-header allowlist for network transports",
    )
    parser.add_argument(
        "--allowed-origins",
        default=read_env("MCP_ALLOWED_ORIGINS"),
        help="Comma-separated exact browser/WebSocket origins; unset blocks Origin requests",
    )
    parser.add_argument(
        "--max-request-bytes",
        type=int,
        default=int(read_env("MCP_MAX_REQUEST_BYTES", str(4 * 1024 * 1024))),
        help="Maximum buffered MCP HTTP request body",
    )
    parser.add_argument(
        "--auth-type",
        default=read_env("AUTH_TYPE"),
        choices=["none", "static", "jwt", "oauth-proxy", "oidc-proxy", "remote-oauth"],
        help="Authentication type for MCP server: 'none' (disabled), 'static' (internal), 'jwt' (external token verification), 'oauth-proxy', 'oidc-proxy', 'remote-oauth' (external) (default: none)",
    )
    parser.add_argument(
        "--static-tokens-ref",
        default=read_env("FASTMCP_SERVER_AUTH_STATIC_TOKENS_REF"),
        help="Secret reference containing the JSON map used by static authentication",
    )
    parser.add_argument(
        "--token-jwks-uri",
        default=read_env("FASTMCP_SERVER_AUTH_JWT_JWKS_URI"),
        help="JWKS URI for JWT verification",
    )
    parser.add_argument(
        "--token-issuer",
        default=read_env("FASTMCP_SERVER_AUTH_JWT_ISSUER"),
        help="Issuer for JWT verification",
    )
    parser.add_argument(
        "--token-audience",
        default=read_env("FASTMCP_SERVER_AUTH_JWT_AUDIENCE"),
        help="Audience for JWT verification",
    )
    parser.add_argument(
        "--token-algorithm",
        default=read_env("FASTMCP_SERVER_AUTH_JWT_ALGORITHM"),
        choices=[
            "HS256",
            "HS384",
            "HS512",
            "RS256",
            "RS384",
            "RS512",
            "ES256",
            "ES384",
            "ES512",
        ],
        help="JWT signing algorithm (required for HMAC or static key). Auto-detected for JWKS.",
    )
    parser.add_argument(
        "--token-secret-ref",
        default=read_env("FASTMCP_SERVER_AUTH_JWT_SECRET_REF"),
        help="Runtime secret reference for an HMAC JWT verification secret",
    )
    parser.add_argument(
        "--token-public-key",
        default=read_env("FASTMCP_SERVER_AUTH_JWT_PUBLIC_KEY"),
        help="Path to PEM public key file or inline PEM string (for static asymmetric keys).",
    )
    parser.add_argument(
        "--required-scopes",
        default=read_env("FASTMCP_SERVER_AUTH_JWT_REQUIRED_SCOPES"),
        help="Comma-separated list of required scopes (e.g., gitlab.read,gitlab.write).",
    )
    parser.add_argument(
        "--oauth-upstream-auth-endpoint",
        default=None,
        help="Upstream authorization endpoint for OAuth Proxy",
    )
    parser.add_argument(
        "--oauth-upstream-token-endpoint",
        default=None,
        help="Upstream token endpoint for OAuth Proxy",
    )
    parser.add_argument(
        "--oauth-upstream-client-id",
        default=None,
        help="Upstream client ID for OAuth Proxy",
    )
    parser.add_argument(
        "--oauth-upstream-client-secret-ref",
        dest="oauth_upstream_client_secret",
        action=RuntimeSecretReferenceAction,
        default=default_oauth_upstream_client_secret,
        help="Runtime secret reference for the OAuth proxy client secret",
    )
    parser.add_argument(
        "--oauth-base-url", default=None, help="Base URL for OAuth Proxy"
    )
    parser.add_argument(
        "--oidc-config-url",
        default=default_oidc_config,
        help="OIDC configuration URL",
    )
    parser.add_argument(
        "--oidc-client-id",
        default=default_oidc_client_id,
        help="OIDC client ID",
    )
    parser.add_argument(
        "--oidc-client-secret-ref",
        dest="oidc_client_secret",
        action=RuntimeSecretReferenceAction,
        default=default_oidc_client_secret,
        help="Runtime secret reference for the OIDC client secret",
    )
    parser.add_argument(
        "--oidc-base-url",
        default=read_env("OIDC_BASE_URL"),
        help="Base URL for OIDC Proxy",
    )
    parser.add_argument(
        "--public-base-url",
        default=read_env("MCP_PUBLIC_BASE_URL"),
        help="Public base URL of this MCP server, used to advertise RFC 9728 "
        "protected-resource metadata for JWT auth (e.g. https://arbora.example) "
        "so an RFC 9728-aware client can discover the authorization server and "
        "refresh its own token instead of a static JWT",
    )
    parser.add_argument(
        "--remote-auth-servers",
        default=None,
        help="Comma-separated list of authorization servers for Remote OAuth",
    )
    parser.add_argument(
        "--remote-base-url", default=None, help="Base URL for Remote OAuth"
    )
    parser.add_argument(
        "--allowed-client-redirect-uris",
        default=None,
        help="Comma-separated list of allowed client redirect URIs",
    )
    parser.add_argument(
        "--eunomia-type",
        default=read_env("EUNOMIA_TYPE"),
        choices=["none", "embedded", "remote"],
        help="Eunomia authorization type: 'none' (disabled), 'embedded' (built-in), 'remote' (external) (default: none)",
    )
    parser.add_argument(
        "--eunomia-policy-file",
        default=read_env("EUNOMIA_POLICY_FILE", "mcp_policies.json"),
        help="Policy file for embedded Eunomia (default: mcp_policies.json)",
    )
    parser.add_argument(
        "--eunomia-remote-url",
        default=read_env("EUNOMIA_REMOTE_URL"),
        help="URL for remote Eunomia server",
    )
    parser.add_argument(
        "--eunomia-api-key-ref",
        default=read_env("EUNOMIA_API_KEY_REF"),
        help="Runtime secret reference for the remote Eunomia API key",
    )
    parser.add_argument(
        "--enable-delegation",
        action="store_true",
        default=to_boolean(read_env("ENABLE_DELEGATION", "False")),
        help="Enable OIDC token delegation",
    )
    parser.add_argument(
        "--audience",
        default=read_env("AUDIENCE"),
        help="Audience for the delegated token",
    )
    parser.add_argument(
        "--delegated-scopes",
        default=read_env("DELEGATED_SCOPES"),
        help="Scopes for the delegated token (space-separated)",
    )
    parser.add_argument(
        "--openapi-file",
        default=None,
        help="Path to the OpenAPI JSON file to import additional tools from",
    )
    parser.add_argument(
        "--openapi-base-url",
        default=None,
        help="Base URL for the OpenAPI client (overrides instance URL)",
    )
    parser.add_argument(
        "--openapi-use-token",
        action="store_true",
        help="Use the incoming Bearer token (from MCP request) to authenticate OpenAPI import",
    )

    parser.add_argument(
        "--openapi-username",
        default=read_env("OPENAPI_USERNAME"),
        help="Username for basic auth during OpenAPI import",
    )

    parser.add_argument(
        "--openapi-password-ref",
        dest="openapi_password",
        action=RuntimeSecretReferenceAction,
        default=default_openapi_password,
        help="Runtime secret reference for OpenAPI basic authentication",
    )

    parser.add_argument(
        "--openapi-client-id",
        default=read_env("OPENAPI_CLIENT_ID"),
        help="OAuth client ID for OpenAPI import",
    )

    parser.add_argument(
        "--openapi-client-secret-ref",
        dest="openapi_client_secret",
        action=RuntimeSecretReferenceAction,
        default=default_openapi_client_secret,
        help="Runtime secret reference for the OpenAPI OAuth client secret",
    )

    parser.add_argument(
        "--tools",
        "--toolsets",
        default=read_env("MCP_ENABLED_TOOLS"),
        help="Comma-separated list of enabled tools or toolsets to expose",
    )
    parser.add_argument(
        "--disabled-tools",
        "--disabled-toolsets",
        default=read_env("MCP_DISABLED_TOOLS"),
        help="Comma-separated list of disabled tools or toolsets to exclude",
    )

    parser.add_argument("--help", action="store_true", help="Show usage")
    return parser
