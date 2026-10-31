"""CONCEPT:MERISTEM-ECO.messaging.native-backend-abstraction"""

import pytest

from arbora.mcp_server.backing.server_factory import create_mcp_parser

# Configured toolsets are built by arbora, the harness host.
pytestmark = pytest.mark.usefixtures("arbora_harness_host")


def test_mcp_parser_defaults():
    parser = create_mcp_parser()
    args = parser.parse_args([])
    assert args.transport == "stdio"
    # Loopback by default: exposing the server on every interface is an explicit opt-in.
    assert args.host == "127.0.0.1"
    assert args.port == 9000
    assert args.auth_type == "none"


def test_mcp_parser_custom():
    parser = create_mcp_parser()
    args = parser.parse_args(
        ["--transport", "sse", "--port", "9999", "--auth-type", "jwt"]
    )
    assert args.transport == "sse"
    assert args.port == 9999
    assert args.auth_type == "jwt"
