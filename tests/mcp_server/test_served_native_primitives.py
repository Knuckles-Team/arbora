"""The served bridge hides policy-denied prompts and resources from every list (ARBORA-FLEET-R007.3).

A real MCP client talks to a started server whose one listing chokepoint is the
verified-principal policy middleware. The denied prompt, resource and template are
absent from the lists; the allowed ones and the tools stay listed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from arbora.access.action_policy import ActionPolicies
from arbora.access.approval import ApprovalDecisions
from arbora.mcp_server.backing.arbora_surface import register_arbora_surface
from arbora.mcp_server.backing.eunomia_principal import create_eunomia_middleware


def _policy(tmp_path: Path, allowed_names: list[str]) -> str:
    """Eunomia is default-deny: each allowed component name gets one allow rule."""
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "version": "1.0",
                "name": "allow-native-content",
                "default_effect": "deny",
                "rules": [
                    {
                        "name": f"allow-{name}",
                        "effect": "allow",
                        "principal_conditions": [],
                        "resource_conditions": [
                            {
                                "path": "attributes.name",
                                "operator": "equals",
                                "value": name,
                            }
                        ],
                        "actions": ["list", "execute"],
                    }
                    for name in allowed_names
                ],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


async def _listed(server: FastMCP) -> dict[str, set[str]]:
    async with Client(server) as client:
        return {
            "prompts": {p.name for p in await client.list_prompts()},
            "resources": {r.name for r in await client.list_resources()},
            "templates": {t.name for t in await client.list_resource_templates()},
            "tools": {t.name for t in await client.list_tools()},
        }


def _started(policy: str | None) -> FastMCP:
    server = FastMCP("host")
    if policy is not None:
        server.add_middleware(create_eunomia_middleware(policy))
    register_arbora_surface(
        server,
        action_policies=ActionPolicies(),
        approvals=ApprovalDecisions(),
        intent_tools=False,
    )
    return server


@pytest.mark.spec("ARBORA-FLEET-R007.3")
@pytest.mark.asyncio
async def test_served_bridge_native_primitives(tmp_path: Path) -> None:
    open_lists = await _listed(_started(None))
    assert open_lists["prompts"] and open_lists["resources"] and open_lists["templates"]
    everything = sorted(set().union(*open_lists.values()))

    allowed = await _listed(_started(_policy(tmp_path, everything)))
    assert allowed == open_lists

    without_content = [
        n
        for n in everything
        if n not in open_lists["prompts"] | open_lists["resources"]
    ]
    hidden = await _listed(_started(_policy(tmp_path, without_content)))
    assert hidden["prompts"] == set()
    assert hidden["resources"] == set()
    assert hidden["templates"] == open_lists["templates"]
    assert hidden["tools"] == open_lists["tools"]

    without_templates = [n for n in everything if n not in open_lists["templates"]]
    template_denied = await _listed(_started(_policy(tmp_path, without_templates)))
    assert template_denied["templates"] == set()
    assert template_denied["prompts"] == open_lists["prompts"]
