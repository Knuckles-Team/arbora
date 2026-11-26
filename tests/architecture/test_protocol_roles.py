"""One protocol stack, one implementation per role (ARBORA-FLEET-R038).

Arbora owns MCP serving, A2A serving and the multiplexer MCP client. The
scion-sdk owns connector client transports and connector server scaffolding.
An arbora module that imports the SDK's connector-server scaffolding, its
external-agent client, or defines its own connector transport or connector
server is a second implementation of a role it does not own.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "arbora"

#: SDK modules that are the connector-side roles (server scaffolding and the
#: connector's consuming clients); arbora must not build on them.
CONNECTOR_ROLE_MODULES = (
    "scion_sdk.mcp.server",
    "scion_sdk.mcp.upstreams",
    "scion_sdk.a2a.client",
    "scion_sdk.a2a.consumer",
)
#: Names that would be a second connector role implementation inside arbora.
FORBIDDEN_DEFINITIONS = {
    "create_connector_server",
    "ConnectorServer",
    "ConnectorMcpTransport",
    "ConnectorA2ATransport",
    "ExternalAgentClient",
}


def _modules():
    for path in sorted(ROOT.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


@pytest.mark.spec("ARBORA-FLEET-R038")
def test_no_second_role_implementation() -> None:
    offenders: list[str] = []
    for path, tree in _modules():
        rel = path.relative_to(ROOT.parent).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                names = []
            for name in names:
                if any(
                    name == role or name.startswith(role + ".")
                    for role in CONNECTOR_ROLE_MODULES
                ):
                    offenders.append(f"{rel}:{node.lineno} imports {name}")
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and node.name in FORBIDDEN_DEFINITIONS
            ):
                offenders.append(f"{rel}:{node.lineno} defines {node.name}")
    assert offenders == []


@pytest.mark.spec("ARBORA-FLEET-R038")
def test_the_gate_catches_a_second_implementation(tmp_path: Path) -> None:
    sample = ast.parse(
        "from scion_sdk.mcp.server import create_mcp_server\nclass ConnectorServer: ..."
    )
    found = [
        node
        for node in ast.walk(sample)
        if (isinstance(node, ast.ImportFrom) and node.module == "scion_sdk.mcp.server")
        or (isinstance(node, ast.ClassDef) and node.name in FORBIDDEN_DEFINITIONS)
    ]
    assert len(found) == 2
