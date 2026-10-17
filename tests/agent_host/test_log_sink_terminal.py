"""Optional cross-repository receipt: real sink, HTTP adapter and mounted TUI.

Run with both reviewed source trees and their provisioned dependencies. Ordinary
GraphOS-only validation skips this receipt rather than claiming terminal proof.
"""
# spec: GRAPHOS-HOST-R009.8

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

pytest.importorskip(
    "agent_terminal_ui.host_logs", reason="reviewed TUI source required"
)
from agent_terminal_ui.app import AgentApp
from agent_terminal_ui.host_logs import AuthenticatedHostLogReader
from textual.widgets import RichLog

from tests.agent_host.test_log_access import ControlledOwners, serve
from tests.agent_host.test_log_sink import runtime_for


@pytest.mark.asyncio
async def test_production_sink_over_http_into_mounted_terminal(tmp_path: Path) -> None:
    owners = ControlledOwners()
    runtime = runtime_for(owners, tmp_path, True)
    agent = AsyncMock()
    agent.list_capabilities = AsyncMock(
        side_effect=RuntimeError("fixture catalog unavailable")
    )
    try:
        with serve(None, runtime) as server:
            async with httpx.AsyncClient(
                base_url=str(server.base_url), headers={"Authorization": "Bearer a"}
            ) as client:
                assert (await client.get("health")).status_code == 200
                reader = AuthenticatedHostLogReader(client)
                receipt = await reader.bind()
                app = AgentApp(client=agent, host_receipt=receipt, log_reader=reader)
                async with app.run_test(size=(120, 40)) as pilot:
                    await app._cmd_processor.cmd_logs("")
                    await pilot.pause(0.4)
                    log = app.screen.query_one("#server-log", RichLog)
                    assert "Authenticated host request received" in "".join(
                        line.text for line in log.lines
                    )
                    owners.allowed = False
                    await pilot.pause(0.4)
                    text = "".join(line.text for line in log.lines)
                    assert "Authenticated host request received" not in text
                    assert "authenticated host reader is required" in text
                agent.close.assert_not_awaited()
                assert not client.is_closed
    finally:
        runtime.sink.close()
