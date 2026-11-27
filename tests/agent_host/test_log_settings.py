"""The host log bounds come from ``config.json`` and stay inside their ranges (ARBORA-HOST-R009.4)."""

from __future__ import annotations

import pytest

from arbora.agent_host.log_settings import HostLogSettings, host_log_settings


@pytest.mark.spec("ARBORA-HOST-R009.4")
def test_defaults_and_bounds_of_the_host_log_keys() -> None:
    assert host_log_settings({}) == HostLogSettings(10_485_760, 5)
    named = {"host_logs.max_bytes": "65536", "host_logs.keep_files": "50"}
    assert host_log_settings(named) == HostLogSettings(65_536, 50)
    top = {"host_logs.max_bytes": str(1 << 30), "host_logs.keep_files": "1"}
    assert host_log_settings(top) == HostLogSettings(1 << 30, 1)
    for key, value in (
        ("host_logs.max_bytes", "65535"),
        ("host_logs.max_bytes", str((1 << 30) + 1)),
        ("host_logs.keep_files", "0"),
        ("host_logs.keep_files", "51"),
        ("host_logs.keep_files", "many"),
    ):
        with pytest.raises(ValueError, match="invalid_host_log_settings"):
            host_log_settings({key: value})
