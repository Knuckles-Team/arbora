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
``arbora.console:main`` over stdio or authenticated
streamable HTTP. Capability limits that depend on unfinished upstream
contracts are documented in ``docs/status.md`` and fail closed; no legacy
fallback is implied by this package description.
"""

from arbora._version import __version__

from . import epistemic

__all__ = ["__version__", "epistemic"]
