"""Exercise actual FastMCP route mounting, not only a registration recorder."""

import pytest
from fastapi.testclient import TestClient
from fastmcp import FastMCP

from arbora.a2a import mcp as a2a_mcp
from arbora.a2a.service import A2AService
from arbora.mcp_server import runtime
from tests.a2a.test_application import Authenticator, Authority, Router


@pytest.mark.spec("ARBORA-A2A-R001.3.3")
def test_root_and_endpoint_card_aliases_share_authenticated_handler(monkeypatch):
    auth = Authenticator()
    service = A2AService(authority=Authority(), router=Router())
    monkeypatch.setattr(a2a_mcp, "_service", lambda: service)
    monkeypatch.setattr(a2a_mcp, "AmbientA2AAuthenticator", lambda: auth)
    monkeypatch.setattr(runtime, "REGISTERED_TOOLS", {})
    mcp = FastMCP("discovery-parity")
    a2a_mcp.register_a2a_tools(mcp)
    with TestClient(mcp.http_app()) as client:
        paths = ("/.well-known/agent-card.json", "/a2a/.well-known/agent-card.json")
        for path in paths:
            assert client.get(path).status_code == 401
        cards = [
            client.get(path, headers={"Authorization": "Bearer verified"})
            for path in paths
        ]
        assert all(card.status_code == 200 for card in cards)
        assert cards[0].json() == cards[1].json()
        assert cards[0].json()["url"] == "http://testserver/a2a"
        assert all(card.headers["Cache-Control"] == "no-store" for card in cards)
        assert auth.scopes == ["kg:read"] * 4
