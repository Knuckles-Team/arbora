"""A ``ProxiedCalls`` built from plain stand-ins, with no multiplexer."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from arbora.core.settings import RuntimeConfig
from arbora.fleet.proxied_calls import ProxiedCalls


async def _unused(*_args: Any, **_kwargs: Any) -> None:
    return None


def build_proxied_calls(
    *, catalog: dict[str, Any] | None = None, epoch: int = 0, **overrides: Any
) -> ProxiedCalls:
    """Build the collaborator with an empty fleet whose catalog is ``catalog``."""
    arguments: dict[str, Any] = {
        "child_policy_admitted_tools": {},
        "child_runtime_policies": {},
        "child_schema_refresh_errors": {},
        "child_schema_revisions": {},
        "fleet": SimpleNamespace(config=RuntimeConfig()),
        "host_mcp": lambda: None,
        "children": {},
        "credential_authority": lambda: None,
        "workflow_bindings": SimpleNamespace(),
        "catalog": lambda: None,
        "catalog_epoch": lambda: epoch,
        "aggregated_tools": lambda: {},
        "tool_to_server": lambda: {},
        "admit_runtime_policy_tools": _unused,
        "open_one_session": _unused,
        "load_catalog": lambda *_a, **_k: catalog or {},
        "notify_pending_tools_changed": _unused,
        "tool_dispatchable": lambda *_a, **_k: True,
    }
    arguments.update(overrides)
    return ProxiedCalls(**arguments)
