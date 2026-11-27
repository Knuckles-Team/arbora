"""Static answers of the ``arbora`` console command.

``arbora --help``, ``-h`` and ``--version`` are answered here, before
:func:`arbora.mcp_server.server.mcp_server` loads ``config.json``. They never
read configuration, resolve a secret reference, or reach the engine, so an
operator can inspect an installed wheel whose configuration does not load.
This module sits outside the served MCP surface: its stdout is console output,
never protocol output.
"""

from __future__ import annotations

from typing import Any

_PROVISION_COMMANDS = (
    "provision",
    "enrol",
    "normalize-config",
    "probe-sso",
    "check-in-pod",
)


def _deploy_realm_admin(path: Any) -> Any:
    """The realm admin port of the console's deploy config (composed here, not by identity)."""
    from arbora.deployment.provision import DeployConfig, Ports

    return Ports.real(DeployConfig.load(path)).admin


def _run_before_usage(argv: list[str]) -> None:
    """Commands whose own arguments may hold ``--help`` or ``--version`` (a child's flags).

    ``arbora lease run`` passes everything after ``--`` to its child, so the console's
    usage check must not see it.
    """
    if argv[1:3] == ["lease", "run"]:
        from arbora.fleet.lease_runner import main as lease_run_main

        raise SystemExit(lease_run_main(argv[3:]))


def main() -> None:
    """Process composition root: inject concrete browser identity exactly once."""
    import sys

    from arbora.mcp_server.static_answers import print_usage_or_version
    from arbora.observability.state import Errors
    from arbora.security.error_surface import bind_errors

    bind_errors(Errors())  # this command-line root owns the process's error store
    _run_before_usage(sys.argv)
    if print_usage_or_version(sys.argv[1:]):
        return
    if sys.argv[1:2] == ["token"]:
        from arbora.identity.client_enrollment_cli import token_main

        raise SystemExit(token_main(sys.argv[2:]))
    if sys.argv[1:3] == ["clients", "enroll"]:
        from arbora.identity.client_enrollment_cli import enroll_main

        raise SystemExit(
            enroll_main(sys.argv[3:], admin_for_config=_deploy_realm_admin)
        )
    if sys.argv[1:3] == ["identity", "reconcile"]:
        from arbora.identity.client_contract import reconcile_command

        raise SystemExit(reconcile_command(sys.argv[3:]))
    if sys.argv[1:3] == ["deploy", "run"]:
        from arbora.deployment.run import main as deploy_run_main

        raise SystemExit(deploy_run_main(sys.argv[3:]))
    if sys.argv[1:3] == ["acceptance", "run"]:
        from arbora.deployment.acceptance.runner import main as acceptance_main

        raise SystemExit(acceptance_main(sys.argv[2:]))
    if sys.argv[1:3] == ["secrets", "diagnose"]:
        from arbora.security.secret_diagnostics import main as diagnose_main

        raise SystemExit(diagnose_main(sys.argv[3:]))
    if sys.argv[1:3] == ["deploy", "source"]:
        from arbora.deployment.deployed_source import main as deployed_source_main

        raise SystemExit(deployed_source_main(sys.argv[3:]))
    if (
        sys.argv[1:2] == ["deploy"]
        and sys.argv[2:3]
        and sys.argv[2] in _PROVISION_COMMANDS
    ):
        from arbora.deployment.provision import main as provision_main

        raise SystemExit(provision_main(sys.argv[2:]))
    if sys.argv[1:3] == ["skills", "export"]:
        from arbora.fleet.agent_surface_cli import main as skills_export_main

        raise SystemExit(skills_export_main(sys.argv[3:]))
    if sys.argv[1:3] == ["deploy", "render"]:
        from arbora.deployment.cli import main as deployment_main

        raise SystemExit(deployment_main(["render", *sys.argv[3:]]))
    if sys.argv[1:2] == ["plugin"]:
        from arbora.harness_plugins.cli import main as plugin_main

        raise SystemExit(plugin_main(sys.argv[2:]))
    from functools import partial

    from arbora.identity.client_contract import (
        start_client_contract_supervisor,
        stop_client_contract_supervisor,
    )
    from arbora.identity.ports import IdentityLifecycle
    from arbora.mcp_server.browser_composition import configured_browser_boundary
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
