"""Arbora-owned hosting/deployment configuration settings (ARBORA-HOST-R010).

A typed, validated model for the hosting and deployment settings that
Arbora owns in its own right -- split out of the agent runtime's
monolithic ``meristem.core.config`` module. Connector base settings
belong to the connector SDK's own configuration module; agent/model
settings remain with the agent runtime. This module resolves without
importing ``meristem.core.config`` internals.

Existing call sites across ``arbora/gateway`` and ``arbora/messaging``
still read settings through ``meristem.core.config`` directly; this
is the typed model for the first slice of the migration (ARBORA-HOST-R010
``.1``). Routing each call site onto this module is follow-up work.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass


class InvalidHostConfigError(ValueError):
    """A hosting/deployment setting failed validation."""


@dataclass(frozen=True)
class HostingDeploymentSettings:
    """Arbora's own view of its hosting and deployment settings."""

    bind_host: str
    bind_port: int
    public_base_url: str
    deployment_environment: str

    def __post_init__(self) -> None:
        if not self.bind_host.strip():
            raise InvalidHostConfigError("bind_host must not be empty")
        if not (0 < self.bind_port < 65536):
            raise InvalidHostConfigError(
                f"bind_port must be between 1 and 65535, got {self.bind_port}"
            )
        if not self.public_base_url.strip():
            raise InvalidHostConfigError("public_base_url must not be empty")
        if self.deployment_environment not in {"development", "staging", "production"}:
            raise InvalidHostConfigError(
                "deployment_environment must be one of "
                "'development', 'staging', 'production', got "
                f"{self.deployment_environment!r}"
            )


def load_hosting_deployment_settings(
    env: dict[str, str] | None = None,
) -> HostingDeploymentSettings:
    """Resolve hosting/deployment settings from Arbora's own environment
    variables only -- never from ``meristem.core.config`` internals."""
    source = env if env is not None else os.environ
    return HostingDeploymentSettings(
        bind_host=source.get("ARBORA_BIND_HOST", "127.0.0.1"),
        bind_port=int(source.get("ARBORA_BIND_PORT", "8000")),
        public_base_url=source.get("ARBORA_PUBLIC_BASE_URL", "http://localhost:8000"),
        deployment_environment=source.get(
            "ARBORA_DEPLOYMENT_ENVIRONMENT", "development"
        ),
    )


class UnknownServeFlag(InvalidHostConfigError):
    """The serve command line carries a flag the host parser does not know."""


TRANSPORTS = ("stdio", "streamable-http", "sse")


@dataclass(frozen=True)
class ServeArguments:
    """The parsed serve command line of ``arbora``."""

    transport: str
    host: str
    port: int
    auth_type: str | None
    token_jwks_uri: str | None
    token_issuer: str | None
    token_audience: str | None


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UnknownServeFlag(message)


def parse_serve_argv(argv: list[str]) -> ServeArguments:
    """Parse ``arbora`` serve arguments; the leading program name is optional.

    A flag this parser does not know raises :class:`UnknownServeFlag`, so a
    documented example that names an obsolete flag fails instead of passing.
    """
    parser = _Parser(prog="arbora", add_help=False, allow_abbrev=False)
    parser.add_argument("--transport", choices=TRANSPORTS, default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--auth-type", default=None)
    parser.add_argument("--token-jwks-uri", default=None)
    parser.add_argument("--token-issuer", default=None)
    parser.add_argument("--token-audience", default=None)
    arguments = argv[1:] if argv[:1] == ["arbora"] else argv
    parsed = parser.parse_args(arguments)
    return ServeArguments(
        transport=parsed.transport,
        host=parsed.host,
        port=parsed.port,
        auth_type=parsed.auth_type,
        token_jwks_uri=parsed.token_jwks_uri,
        token_issuer=parsed.token_issuer,
        token_audience=parsed.token_audience,
    )


def main() -> int:
    """CLI entry point: print the resolved hosting/deployment settings."""
    settings = load_hosting_deployment_settings()
    print(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
