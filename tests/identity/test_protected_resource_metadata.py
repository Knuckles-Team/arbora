"""Protected-resource metadata (ARBORA-IDENTITY-R030.3)."""

from __future__ import annotations

import argparse

import pytest

from arbora.identity.ports import native_signin_scopes
from arbora.mcp_server.backing.server_auth import _wrap_with_resource_metadata


@pytest.mark.spec("ARBORA-IDENTITY-R030.3")
def test_unauthenticated_request_points_at_metadata() -> None:
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    verifier = object()
    args = argparse.Namespace(public_base_url="https://arbora.example.test")
    provider = _wrap_with_resource_metadata(
        verifier, args, ["https://issuer.example.test/realms/arbora"]
    )
    routes = provider.get_routes("/mcp")
    paths = {getattr(route, "path", "") for route in routes}
    assert "/.well-known/oauth-protected-resource/mcp" in paths
    app = Starlette(routes=routes)
    document = TestClient(app).get("/.well-known/oauth-protected-resource/mcp").json()
    assert document["authorization_servers"] == [
        "https://issuer.example.test/realms/arbora"
    ]
    assert set(native_signin_scopes()) <= set(document["scopes_supported"])
