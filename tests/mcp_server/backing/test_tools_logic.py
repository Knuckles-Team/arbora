"""Tests for MCP tool loading, parsing, and configuration.

CONCEPT:AU-ECO.messaging.native-backend-abstraction — Agent Tool System
"""

import pytest

from arbora.mcp_server.backing.server_factory import create_mcp_parser

# Configured toolsets are built by arbora, the harness host.
pytestmark = pytest.mark.usefixtures("arbora_harness_host")


def test_mcp_parser_defaults():
    """Test MCP argument parser default values."""
    parser = create_mcp_parser()
    args = parser.parse_args([])
    assert args.transport == "stdio"
    assert args.auth_type == "none"
