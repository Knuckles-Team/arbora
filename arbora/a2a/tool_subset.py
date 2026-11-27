"""The caller-filtered tool subset of a routed run (ARBORA-A2A-R002.2.1).

The harness carries and enforces the subset: with ``HarnessConfig.action_space`` set to
``{"tool_subset": [...]}`` a task call outside the list refuses ``TOOL_NOT_ALLOWED``
before any tool runs. Arbora

* reads the routing decision's ``selected_tools`` from the durable WorkItem route,
* sets that carrier on the run plan (``plan_for_owner``'s sibling, read by ``decide_run``),
  never a list it computed itself, and
* refuses a selected subset at admission when the harness has no carrier, so nothing is
  ever passed through unenforced (``ARBORA-A2A-R002.1``).

PIN-LAG: the pinned harness has no ``action_space`` field, so every selected subset
refuses at admission until the re-pin that carries it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "REFUSAL",
    "carrier_available",
    "plan_with_subset",
    "require_carrier",
    "selected_tools_of",
    "with_tool_subset",
]

REFUSAL = "canonical agent dispatch cannot yet enforce an assembled tool subset"


def carrier_available() -> bool:
    """Whether the installed harness carries and enforces a tool subset."""
    try:
        from meristem.graph.state import HarnessConfig
    except ImportError:  # PIN-LAG
        return False
    return "action_space" in {f.name for f in dataclasses.fields(HarnessConfig)}


def require_carrier(selected_tools: Sequence[str]) -> None:
    """Refuse a selected subset the harness cannot enforce, before durable admission."""
    if selected_tools and not carrier_available():
        from .routing import A2AAssemblyUnavailable

        raise A2AAssemblyUnavailable(REFUSAL)


def with_tool_subset(plan: Any, subset: Sequence[str]) -> Any:
    """``plan`` with ``harness.action_space['tool_subset']`` set to ``subset``."""
    if not subset:
        return plan
    require_carrier(subset)
    harness = plan.harness
    space = {**(harness.action_space or {}), "tool_subset": list(subset)}
    return dataclasses.replace(
        plan, harness=dataclasses.replace(harness, action_space=space)
    )


def selected_tools_of(item: Mapping[str, Any]) -> tuple[str, ...]:
    """The routing decision's selected tools, as the WorkItem recorded them."""
    route = (item.get("metadata") or {}).get("a2a_route") or {}
    tools = route.get("selectedTools") or route.get("selected_tools") or ()
    return tuple(str(tool) for tool in tools)


def plan_with_subset(plan: Any, owner: Any) -> Any:
    """``plan`` carrying the tool subset of the native run ``owner``, when it has one."""
    return with_tool_subset(plan, getattr(owner, "tool_subset", ()) or ())
