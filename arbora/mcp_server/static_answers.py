"""Static answers of the ``arbora`` command (``--help``, ``-h``, ``--version``).

They are answered before :func:`arbora.mcp_server.server.mcp_server` loads ``config.json``:
they never read configuration, resolve a secret reference, or reach the engine, so an
operator can inspect an installed wheel whose configuration does not load. The output is
console output, never protocol output.
"""

from __future__ import annotations

__all__ = ["print_usage_or_version"]


def print_usage_or_version(argv: list[str]) -> bool:
    """Print usage or the version for ``argv`` and return whether it did."""
    if "--version" in argv:
        from arbora.version import __version__

        print(f"arbora {__version__}")
        return True
    if "--help" in argv or "-h" in argv:
        from arbora.mcp_server.backing.server_parser import create_mcp_parser

        parser = create_mcp_parser(resolve_secret_defaults=False)
        parser.prog = "arbora"
        parser.epilog = (
            "command: arbora lease run --pool CELL --priority land-gate|train|lane "
            "[--wait-secs N] [--ttl-ms N] -- ARGV...  (run a command under one "
            "build-slot lease)"
        )
        parser.add_argument("--version", action="store_true", help="Show the version")
        parser.print_help()
        return True
    return False
