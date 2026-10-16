"""Graph OS session port over the agent-utilities harness session authority.

Moved from the agent-utilities ``api`` session module when 3.0.0 was cut
to the agent harness only (no ``api``/``mcp``/``gateway``/``server``
packages). Authentication middleware remains responsible for minting and
binding a verified session; this module is the single Graph OS import seam
for that harness-owned contract, so every Graph OS surface (MCP, REST, A2A,
WebUI host) binds the same session objects without importing the harness's
knowledge-graph internals directly.
"""

from agent_utilities.knowledge_graph.core.session import (
    GraphSession,
    ScopeError,
    SessionExpiredError,
    SessionRequiredError,
    current_session,
    reset_session,
    resolve_session,
    set_session,
    suspend_session,
    use_session,
)

__all__ = [
    "GraphSession",
    "ScopeError",
    "SessionExpiredError",
    "SessionRequiredError",
    "current_session",
    "reset_session",
    "resolve_session",
    "set_session",
    "suspend_session",
    "use_session",
]
