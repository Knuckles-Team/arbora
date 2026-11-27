"""MCP registration and REST-parity contract for browser control."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.domain.browser_control.browser_control_api import BrowserLeaseReceipt
from arbora.browser_control import mcp as browser_control_mcp
from arbora.browser_control.browser_state import BrowserState
from arbora.core.settings import RuntimeConfig
from arbora.mcp_server import runtime
from arbora.mcp_server.host_caches import ToolTable
from arbora.security.process_authority import ProcessAuthority


def _recording_mcp(tools: dict[str, Any]) -> SimpleNamespace:
    def tool(*, name: str, **_kwargs: Any) -> Any:
        def decorate(function: Any) -> Any:
            tools[name] = function
            return function

        return decorate

    return SimpleNamespace(tool=tool)


@pytest.mark.asyncio
async def test_browser_control_registers_one_mcp_rest_workflow_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def dispatch(
        browser: Any, action: str, payload: dict[str, Any]
    ) -> BrowserLeaseReceipt:
        seen.append((action, payload))
        return BrowserLeaseReceipt(
            lease_id="browserlease_0123456789abcdef0123456789abcdef",
            document_ref=str(payload["document_ref"]),
            tool_ids=tuple(payload["tool_ids"]),
            registration_generation=1,
            expires_at=100.0,
            hard_expires_at=200.0,
            status="active",
        )

    monkeypatch.setattr(browser_control_mcp, "dispatch_browser_control", dispatch)
    tools: dict[str, Any] = {}
    mcp = _recording_mcp(tools)
    table = ToolTable(ProcessAuthority(config=RuntimeConfig()))
    try:
        browser_control_mcp.register_browser_control_tools(mcp, table, BrowserState())
        assert table["browser_control"] is tools["browser_control"]
        assert runtime.ACTION_TOOL_ROUTES["browser_control"] == "/browser/control"
        raw = await tools["browser_control"](
            action="issue_lease",
            payload={
                "document_ref": "document_" + "a" * 64,
                "tool_ids": ["overstory.get-page-context"],
                "attended": True,
            },
        )
        assert json.loads(raw)["status"] == "active"
        assert seen == [
            (
                "issue_lease",
                {
                    "document_ref": "document_" + "a" * 64,
                    "tool_ids": ["overstory.get-page-context"],
                    "attended": True,
                },
            )
        ]
    finally:
        table.pop("browser_control", None)
