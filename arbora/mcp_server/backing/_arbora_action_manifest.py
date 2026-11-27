"""The arbora verbose 1:1 tool surface (CONCEPT:MERISTEM-ECO.mcp.tool-mode-standardization).

One entry per CRUD action over the API gateway action core. Each becomes an MCP tool
``name`` that dispatches ``_execute_tool(tool, action=action, **params)``; ``action=None``
is a single-operation tool. The entries live in two generated table modules, one per
action family, and are joined here in name order. Regenerate with
scripts/gen_arbora_manifest.py after changing the arbora tool surface.
"""

from arbora.mcp_server.backing._arbora_action_engine import ENGINE_ACTIONS
from arbora.mcp_server.backing._arbora_action_surface import SURFACE_ACTIONS
from arbora.mcp_server.backing._arbora_action_type import ArboraAction

ARBORA_ACTIONS: tuple[ArboraAction, ...] = tuple(
    sorted((*SURFACE_ACTIONS, *ENGINE_ACTIONS), key=lambda action: action["name"])
)
