"""The ephemeral deployed stack (ARBORA-ACCEPTANCE-R006.6).

The structure test runs in every pull request. The readiness test starts the stack with
``docker compose`` and runs in the nightly job, where the images exist.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "ephemeral" / "compose.yaml"
READY_BUDGET_S = 120


def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.6")
def test_stack_is_disposable_loopback_only_and_names_three_fixture_connectors() -> None:
    services = _services()
    assert set(services) == {
        "arbora",
        "fixture-orders",
        "fixture-vulns",
        "fixture-tools",
    }
    arbora = services["arbora"]
    assert all(port.startswith("127.0.0.1:") for port in arbora["ports"])
    # The store is memory-backed: there is no named volume to outlive the stack.
    assert "volumes" not in yaml.safe_load(COMPOSE.read_text())
    assert "/var/lib/arbora" in arbora["tmpfs"]
    # The production image: an immutable reference the caller must supply.
    assert "${ARBORA_IMAGE:?" in arbora["image"]
    kinds = {
        name: services[name]["environment"]["FIXTURE_KIND"]
        for name in services
        if name != "arbora"
    }
    assert sorted(kinds.values()) == ["tools", "virtual-graph", "virtual-graph"]
    assert all(
        services[name]["depends_on"]["arbora"]["condition"] == "service_healthy"
        for name in kinds
    )


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.6")
@pytest.mark.skipif(
    shutil.which("docker") is None or not os.environ.get("ARBORA_IMAGE"),
    reason="the nightly job supplies docker and the commit's built images",
)
def test_compose_stack_ready_within_budget() -> None:
    command = ["docker", "compose", "-f", str(COMPOSE)]
    started = time.monotonic()
    try:
        subprocess.run(
            [*command, "up", "--wait", "--wait-timeout", str(READY_BUDGET_S)],
            check=True,
            timeout=READY_BUDGET_S + 60,
        )
        assert time.monotonic() - started <= READY_BUDGET_S + 30
    finally:
        subprocess.run([*command, "down", "--volumes"], check=False, timeout=120)
