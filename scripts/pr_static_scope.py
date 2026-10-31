#!/usr/bin/env python3
"""Print the static architecture and import-scan tests every pull request runs.

The pull-request fast subset narrows the suite to the tests a diff maps to, so
a change that breaks a repository-wide rule from a file the rule's own test does
not map to would pass the pull request and fail on main. These tests read source
and manifests only (about a minute together), so the release workflow runs them
on every pull request whatever the diff touches.

One path per line, relative to the repository root. ``tests/architecture`` is
included whole; the rest are listed in ``STATIC_SCAN_TESTS``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ARCHITECTURE_DIR = "tests/architecture"

STATIC_SCAN_TESTS: tuple[str, ...] = (
    "tests/a2a/test_first_party_facade.py",
    "tests/control_plane/test_boundary.py",
    "tests/core/test_providers.py",
    "tests/deployment/test_host_config.py",
    "tests/deployment/test_real_boot_fixture.py",
    "tests/deployment/test_release_workflow_sources.py",
    "tests/fleet/test_harvest_inventory.py",
    "tests/fleet/test_mcp_resource_receipt_source.py",
    "tests/fleet/test_multiplexer_boundary.py",
    "tests/fleet/test_no_duplicate_skill_prompt_harvest.py",
    "tests/gateway/test_au_widget_connector_imports.py",
    "tests/gateway/test_widget_fleet_delegation.py",
    "tests/identity/test_identity_ports_boundary.py",
    "tests/identity/test_port_only_dependents.py",
    "tests/mcp_server/test_engine_wiring_gate.py",
    "tests/mcp_server/test_late_gateway_parity.py",
    "tests/mcp_server/test_task_plan_receipts.py",
    "tests/test_component_registry_boundary_seeds.py",
    "tests/test_connector_pack_typed_publisher.py",
    "tests/test_package_layout.py",
)

# A test file that parses or walks repository source is a static scan.
SCAN_MARKERS = re.compile(
    r"ast\.(?:parse|walk)\(|rglob\(.\*\.py|\.glob\(.\*\*/\*\.py|import_scan"
)


def static_scan_tests(root: Path = ROOT) -> list[str]:
    """Every static-scan test file, as sorted repository-relative paths."""
    architecture = {
        path.relative_to(root).as_posix()
        for path in (root / ARCHITECTURE_DIR).rglob("test_*.py")
    }
    return sorted(architecture | set(STATIC_SCAN_TESTS))


def main() -> int:
    missing = [path for path in static_scan_tests() if not (ROOT / path).is_file()]
    if missing:
        print(f"pr_static_scope: listed tests do not exist: {missing}", file=sys.stderr)
        return 1
    print("\n".join(static_scan_tests()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
