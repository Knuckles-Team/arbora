"""The arbora backing surface serves native prompts, resources and templates (ARBORA-FLEET-R007.2)."""

from __future__ import annotations

import json

import pytest
from fastmcp import Client, FastMCP

from arbora.access.action_policy import ActionPolicies
from arbora.access.approval import ApprovalDecisions
from arbora.mcp_server.backing.arbora_surface import register_arbora_surface
from arbora.mcp_server.host_caches import HostCaches
from arbora.observability.state import build_health_state


@pytest.mark.spec("ARBORA-FLEET-R007.2")
@pytest.mark.asyncio
async def test_backing_surface_exposes_prompts_resources_and_templates() -> None:
    mcp = FastMCP("host")
    backing = register_arbora_surface(
        mcp,
        action_policies=ActionPolicies(),
        approvals=ApprovalDecisions(),
        engine_provider=lambda: None,
        caches=HostCaches(),
        intent_tools=False,
        health=build_health_state(),
    )
    async with Client(backing) as client:
        prompts = {prompt.name for prompt in await client.list_prompts()}
        resources = {str(item.uri) for item in await client.list_resources()}
        templates = {
            item.uriTemplate for item in await client.list_resource_templates()
        }
        tools = {tool.name for tool in await client.list_tools()}
        status = await client.read_resource("arbora://status")
        described = await client.read_resource("arbora://capability/find")
    async with Client(mcp) as host:  # the composed server loop serves them too
        host_prompts = {prompt.name for prompt in await host.list_prompts()}
        host_resources = {str(item.uri) for item in await host.list_resources()}
    assert (host_prompts, host_resources) == (prompts, resources)
    assert prompts == {"arbora-status"}
    assert resources == {"arbora://status"}
    assert templates == {"arbora://capability/{capability}"}
    assert tools  # the native primitives sit beside the registered tools
    assert json.loads(status[0].text) == {"server": "arbora", "status": "ok"}
    assert json.loads(described[0].text) == {
        "server": "arbora",
        "capability": "find",
    }
