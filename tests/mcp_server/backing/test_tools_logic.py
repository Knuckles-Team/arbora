"""Tests for MCP tool loading, parsing, and configuration.

CONCEPT:MERISTEM-ECO.messaging.native-backend-abstraction — Agent Tool System
"""

from arbora.mcp_server.backing.server_factory import create_mcp_parser


def test_mcp_parser_defaults():
    """Test MCP argument parser default values."""
    parser = create_mcp_parser()
    args = parser.parse_args([])
    assert args.transport == "stdio"
    assert args.auth_type == "none"
