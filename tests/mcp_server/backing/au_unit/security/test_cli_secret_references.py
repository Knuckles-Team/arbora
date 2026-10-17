"""The served MCP CLI resolves credentials from references, never from argv."""

from __future__ import annotations

import argparse

import pytest

from graph_os.mcp_server.backing.server_factory import create_mcp_parser


def _options(parser: argparse.ArgumentParser) -> set[str]:
    return {
        option
        for action in parser._actions  # noqa: SLF001 - inspect the public CLI contract
        for option in action.option_strings
    }


def test_mcp_cli_exposes_only_reference_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "TEST_OAUTH_SECRET": "oauth-runtime",
        "TEST_OIDC_SECRET": "oidc-runtime",
        "TEST_OPENAPI_PASSWORD": "openapi-password-runtime",
        "TEST_OPENAPI_SECRET": "openapi-oauth-runtime",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    parser = create_mcp_parser()
    argv = [
        "--oauth-upstream-client-secret-ref",
        "env://TEST_OAUTH_SECRET",
        "--oidc-client-secret-ref",
        "env://TEST_OIDC_SECRET",
        "--openapi-password-ref",
        "env://TEST_OPENAPI_PASSWORD",
        "--openapi-client-secret-ref",
        "env://TEST_OPENAPI_SECRET",
    ]
    args = parser.parse_args(argv)

    assert args.oauth_upstream_client_secret == values["TEST_OAUTH_SECRET"]
    assert args.oidc_client_secret == values["TEST_OIDC_SECRET"]
    assert args.openapi_password == values["TEST_OPENAPI_PASSWORD"]
    assert args.openapi_client_secret == values["TEST_OPENAPI_SECRET"]
    for value in values.values():
        assert value not in " ".join(argv)
    assert {
        "--token-secret",
        "--oauth-upstream-client-secret",
        "--oidc-client-secret",
        "--openapi-password",
        "--openapi-client-secret",
    }.isdisjoint(_options(parser))


def test_mcp_environment_defaults_resolve_secret_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DEFAULT_OIDC_SECRET", "oidc-default-runtime")
    monkeypatch.setenv("TEST_DEFAULT_OAUTH_SECRET", "oauth-default-runtime")
    monkeypatch.setenv("TEST_DEFAULT_OPENAPI_PASSWORD", "openapi-password-runtime")
    monkeypatch.setenv("TEST_DEFAULT_OPENAPI_SECRET", "openapi-secret-runtime")
    monkeypatch.setenv("OIDC_CLIENT_SECRET_REF", "env://TEST_DEFAULT_OIDC_SECRET")
    monkeypatch.setenv(
        "OAUTH_UPSTREAM_CLIENT_SECRET_REF", "env://TEST_DEFAULT_OAUTH_SECRET"
    )
    monkeypatch.setenv("OPENAPI_PASSWORD_REF", "env://TEST_DEFAULT_OPENAPI_PASSWORD")
    monkeypatch.setenv("OPENAPI_CLIENT_SECRET_REF", "env://TEST_DEFAULT_OPENAPI_SECRET")

    args = create_mcp_parser().parse_args([])

    assert args.oidc_client_secret == "oidc-default-runtime"
    assert args.oauth_upstream_client_secret == "oauth-default-runtime"
    assert args.openapi_password == "openapi-password-runtime"
    assert args.openapi_client_secret == "openapi-secret-runtime"
