"""Configuration-aware messaging intake health (ARBORA-MESSAGING-R005.1)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from arbora.messaging.health import messaging_intake_component
from arbora.messaging.supervision import AdapterImportFailed, AdapterMissing


class _Backend:
    def __init__(self, connected: bool) -> None:
        self.is_connected = connected


class _Registry:
    def __init__(
        self,
        configured: dict[str, bool],
        degraded: dict[str, Exception] | None = None,
    ) -> None:
        self._configured = configured
        self._degraded = degraded or {}

    def list_backends(self) -> list[str]:
        return ["discord", "slack", "telegram"]

    def configured_backend_ids(self) -> list[str]:
        return sorted(self._configured)

    def get_backend(self, backend_id: str) -> Any:
        return _Backend(self._configured[backend_id])

    def degraded_reason(self, backend_id: str) -> Exception | None:
        return self._degraded.get(backend_id)


@pytest.mark.spec("ARBORA-MESSAGING-R005.1")
def test_states_alert_eligibility_and_safe_output() -> None:
    # Disabled: credentials alone never enable intake, and never alert.
    off = messaging_intake_component(_Registry({"telegram": True}), enabled=False)
    assert (off["state"], off["enabled"], off["alert_eligible"]) == (
        "disabled",
        False,
        False,
    )
    assert {row["state"] for row in off["backends"] if row["configured"]} == {
        "disabled"
    }

    # Enabled with nothing configured: not_configured, and alert eligible.
    empty = messaging_intake_component(_Registry({}), enabled=True)
    assert (empty["state"], empty["configured"], empty["alert_eligible"]) == (
        "not_configured",
        False,
        True,
    )

    healthy = messaging_intake_component(_Registry({"telegram": True}), enabled=True)
    assert (healthy["state"], healthy["alert_eligible"]) == ("healthy", False)

    degraded = messaging_intake_component(
        _Registry(
            {"telegram": True, "slack": False, "discord": False},
            {
                "slack": AdapterMissing("secret-detail"),
                "discord": AdapterImportFailed("x"),
            },
        ),
        enabled=True,
    )
    assert (degraded["state"], degraded["alert_eligible"]) == ("degraded", True)
    rows = {row["id"]: row for row in degraded["backends"]}
    assert list(rows) == sorted(rows)
    assert rows["slack"]["reason_code"] == "adapter_missing"
    assert rows["discord"]["reason_code"] == "adapter_import_failed"
    assert rows["telegram"]["reason_code"] is None
    # No raw exception text and no credential value ever leaves the component.
    assert "secret-detail" not in json.dumps(degraded)

    unconnected = messaging_intake_component(_Registry({"slack": False}), enabled=True)
    assert unconnected["backends"][1]["reason_code"] == "backend_not_connected"


@pytest.mark.spec("ARBORA-MESSAGING-R005.1")
def test_health_report_carries_the_component_only_in_the_full_report() -> None:
    from arbora.observability import runtime_health as rh

    report = rh.collect_health()
    assert "messaging_intake" in report["components"]
