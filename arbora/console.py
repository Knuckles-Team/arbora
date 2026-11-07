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
        from arbora.version import __version__

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


def main() -> None:
    """Process composition root: inject concrete browser identity exactly once."""
    import sys

    if print_usage_or_version(sys.argv[1:]):
        return
    if sys.argv[1:3] == ["identity", "reconcile"]:
        from arbora.identity.client_contract import reconcile_command

        raise SystemExit(reconcile_command(sys.argv[3:]))
    if sys.argv[1:3] == ["deploy", "source"]:
        from arbora.deployment.deployed_source import main as deployed_source_main

        raise SystemExit(deployed_source_main(sys.argv[3:]))
    from functools import partial

    from arbora.identity.browser_composition import configured_browser_boundary
    from arbora.identity.client_contract import (
        start_client_contract_supervisor,
        stop_client_contract_supervisor,
    )
    from arbora.identity.ports import IdentityLifecycle
    from arbora.mcp_server.browser_dispatch import dispatch_browser_request
    from arbora.mcp_server.server import mcp_server

    mcp_server(
        browser_boundary_factory=partial(
            configured_browser_boundary, request_dispatch=dispatch_browser_request
        ),
        identity_lifecycle=IdentityLifecycle(
            start=start_client_contract_supervisor,
            stop=stop_client_contract_supervisor,
        ),
    )
