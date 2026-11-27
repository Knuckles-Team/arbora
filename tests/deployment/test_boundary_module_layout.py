"""Arbora hosts its own boundary modules (ARBORA-DEPLOY-R008)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ARBORA_ROOT = Path(__file__).resolve().parents[2] / "arbora"

HOSTED = (
    "security/request_identity.py",
    "security/error_surface.py",
    "security/security_policy_middleware.py",
    "fleet/health.py",
    "core/config_admin.py",
    "mcp_server/backing/agent_config_admin.py",
    "ecosystem/hook_installer.py",
    "ecosystem/agent_manager_dashboard.py",
    "deployment/profile_guard.py",
    "deployment/live_mount_guard.py",
    "deployment/release_canary.py",
    "architecture/boundary_census.py",
)

RETIRED_RUNTIME = "agent_" + "utilities"


def _imported_modules(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
    return found


@pytest.mark.spec("ARBORA-DEPLOY-R008")
def test_each_named_module_exists_and_is_imported_locally() -> None:
    missing = [name for name in HOSTED if not (ARBORA_ROOT / name).is_file()]
    assert missing == []
    stems = {Path(name).stem for name in HOSTED}
    foreign: list[str] = []
    for path in ARBORA_ROOT.rglob("*.py"):
        for module in _imported_modules(path):
            root, _, rest = module.partition(".")
            if root == RETIRED_RUNTIME:
                foreign.append(f"{path.relative_to(ARBORA_ROOT)}: {module}")
            elif root != "arbora" and stems & set(rest.split(".")):
                foreign.append(f"{path.relative_to(ARBORA_ROOT)}: {module}")
    assert foreign == []
