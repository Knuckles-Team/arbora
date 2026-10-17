"""Static answers of the ``arbora`` console command.

``arbora --help``, ``-h`` and ``--version`` are answered here, before
:func:`arbora.mcp_server.server.mcp_server` loads ``config.json``. They never
read configuration, resolve a secret reference, or reach the engine, so an
operator can inspect an installed wheel whose configuration does not load.
This module sits outside the served MCP surface: its stdout is console output,
never protocol output.
"""

from __future__ import annotations


def print_usage_or_version(argv: list[str]) -> bool:
    """Print usage or the version for ``argv`` and return whether it did."""
    if "--version" in argv:
        from arbora._version import __version__

        print(f"arbora {__version__}")
        return True
    if "--help" in argv or "-h" in argv:
        from arbora.mcp_server.backing.server_factory import create_mcp_parser

        parser = create_mcp_parser(resolve_secret_defaults=False)
        parser.prog = "arbora"
        parser.add_argument("--version", action="store_true", help="Show the version")
        parser.print_help()
        return True
    return False
