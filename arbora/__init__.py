"""Arbora: the deployable composition for the Knuckles agent platform.

RF-ADR-009 (`plans/refactor/RF-ADR-009-connector-sdk-and-arbora.md`) §2 assigns
this repository phase 5: **the deployable composition** — the MCP server, REST
gateway, control plane, fleet gateway, agent-webui hosting, and deployment
tooling. It must never own business logic that belongs to
epistemic-graph, agent-connector-sdk, or the agent-utilities agent plane.

The live package layout is:

- `arbora.mcp_server` — native MCP/REST composition and serving lifecycle.
- `arbora.fleet` — in-process MCP fleet gateway and child lifecycle.
- `arbora.gateway` — REST gateway and dashboard projections.
- `arbora.control_plane` — reconciliation and action policy.
- `arbora.webui_host` — optional Agent WebUI co-service hosting.
- `arbora.deployment` — configuration, diagnostics, canaries, and operations.

The ``arbora`` console command serves
``arbora.mcp_server.server:mcp_server`` over stdio or authenticated
streamable HTTP. Capability limits that depend on unfinished upstream
contracts are documented in ``docs/status.md`` and fail closed; no legacy
fallback is implied by this package description.
"""

import os as _os
from pathlib import Path as _Path

from arbora._version import __version__

from . import epistemic

#: The platform-directory variable agent-connector-sdk and agent-utilities still
#: read; they resolve ``mcp_config.json`` and the other shared files through it.
_SHARED_CONFIG_DIR_SETTING = "GRAPH_OS_CONFIG_DIR"  # arbora-rename: keep


def _bridge_shared_config_dir() -> None:
    """Point the shared path helpers at the Arbora directory while it is in use.

    For the one-release configuration fallback only (stdlib-only, so the
    dependency-free client install keeps importing): when ``ARBORA_CONFIG_DIR``
    is set, or ``$XDG_CONFIG_HOME/arbora`` exists, and the previous project's
    variable is unset, that variable is set to the Arbora directory so the SDK
    and agent-utilities read the same files. Otherwise the shared helpers keep
    their own resolution, the legacy location ``arbora.core.settings`` falls
    back to.
    """
    if _os.environ.get(_SHARED_CONFIG_DIR_SETTING):
        return
    explicit = _os.environ.get("ARBORA_CONFIG_DIR", "").strip()
    config_home = _os.environ.get("XDG_CONFIG_HOME") or str(_Path.home() / ".config")
    directory = (
        _Path(explicit).expanduser() if explicit else _Path(config_home) / "arbora"
    )
    if explicit or directory.is_dir():
        _os.environ[_SHARED_CONFIG_DIR_SETTING] = str(directory)


_bridge_shared_config_dir()

__all__ = ["__version__", "epistemic"]
