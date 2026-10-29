"""arbora serves the agent-utilities intent contract and adds its own operations."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.mcp_server import runtime


@pytest.mark.spec("ARBORA-A2A-R001.3.3")
@pytest.mark.parametrize("completed_text_history", [False, True])
def test_surface_serves_intent_contract_and_registers_host_operations(
    monkeypatch: pytest.MonkeyPatch,
    completed_text_history: bool,
) -> None:
    from arbora.a2a import mcp as a2a_mcp
    from arbora.browser_control import mcp as browser_mcp
    from arbora.mcp_server.backing import arbora_surface

    served = object()
    backing = object()
    calls: list[tuple[str, Any, Any]] = []
    profiles: list[bool] = []
    from arbora.core.settings import runtime_config

    monkeypatch.setattr(
        runtime_config, "completed_text_history", completed_text_history
    )

    def surface(mcp: Any, **_: Any) -> Any:
        calls.append(("surface", mcp, None))
        runtime.REGISTERED_TOOLS["graph_query"] = object()
        return backing

    monkeypatch.setattr(arbora_surface, "register_arbora_surface", surface)
    monkeypatch.setattr(
        browser_mcp,
        "register_browser_control_tools",
        lambda target: calls.append(("browser", target, None)),
    )

    def register_a2a(mcp: Any, *, tools: Any, completed_text_history: bool) -> None:
        calls.append(("a2a", mcp, tools))
        profiles.append(completed_text_history)

    monkeypatch.setattr(a2a_mcp, "register_a2a_tools", register_a2a)
    from arbora import host_bindings

    monkeypatch.setattr(
        host_bindings,
        "mount_sdk_a2a",
        lambda mcp: calls.append(("sdk_a2a", mcp, None)),
    )
    monkeypatch.setattr(runtime, "REGISTERED_TOOLS", {})

    runtime._register_graph_tool_surface(served)

    assert calls == [
        ("surface", served, None),
        # Browser control and A2A are ``act`` operations on the backing server.
        ("browser", backing, None),
        ("a2a", served, backing),
        ("sdk_a2a", served, None),
    ]
    assert profiles == [completed_text_history]
    # REST dispatches exactly what the intent tools route to.
    assert "graph_query" in runtime.REGISTERED_TOOLS
