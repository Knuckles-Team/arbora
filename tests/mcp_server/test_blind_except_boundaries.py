"""Boundaries that used to carry ``pragma: no cover`` are reached by real calls."""

from __future__ import annotations

import logging
import runpy
import sys
from types import SimpleNamespace

import pytest

from arbora.mcp_server.backing import server_factory, verbose_tools


def test_operation_registry_module_entry_exits_with_a_status(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["operation_registry", "--check"])
    with pytest.raises(SystemExit) as raised:
        runpy.run_module("arbora.mcp_server.operation_registry", run_name="__main__")
    assert raised.value.code in (0, 1)


def test_failing_action_provider_resolves_to_no_actions_and_logs_traceback(caplog):
    def broken() -> list[str]:
        raise RuntimeError("provider down")

    with caplog.at_level(logging.WARNING):
        assert verbose_tools._resolve_action_provider(broken) == []
    assert any(record.exc_info for record in caplog.records)


def test_route_registration_failure_is_logged_with_traceback_and_does_not_raise(caplog):
    mcp = SimpleNamespace()  # no custom_route: an older FastMCP build
    with caplog.at_level(logging.WARNING):
        server_factory._register_operational_routes(mcp, False, None, SimpleNamespace())
    assert any(record.exc_info for record in caplog.records)
