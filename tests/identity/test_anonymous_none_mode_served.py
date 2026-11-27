"""The unsecured-mode indicators (ARBORA-IDENTITY-R004.2).

The indicator reaches the MCP ``initialize`` instructions, the served ``ops.doctor``
report and the doctor auth check in none mode, and none of them outside it.
"""

from __future__ import annotations

from typing import Any

import pytest

from arbora.core.settings import RuntimeConfig
from arbora.deployment.doctor_identity import _check_auth
from arbora.mcp_server.runtime import SERVED_INSTRUCTIONS, _with_unsecured_banner
from arbora.security import anonymous
from tests.api.test_ops_admin_ops import _context, _FakeOpsAdminStore, ops_admin

_OPS = {op.id: op for op in ops_admin.operations()}
BANNER = "Arbora is running in unsecured loopback mode."


def _mode(
    monkeypatch: pytest.MonkeyPatch, mode: str, exposed: bool = False
) -> RuntimeConfig:
    config = RuntimeConfig()
    config.arbora_auth_mode = mode
    if exposed:
        monkeypatch.setenv("ARBORA_AUTH_NONE_EXPOSE", "acknowledged")
    else:
        monkeypatch.delenv("ARBORA_AUTH_NONE_EXPOSE", raising=False)
    return config


async def _doctor(config: RuntimeConfig) -> dict[str, Any]:
    context = _context(store=_FakeOpsAdminStore())
    context.services["config"] = config
    result = await ops_admin.handle_ops_doctor(context, {}, _OPS["ops.doctor"])
    return dict(result["value"]["checks"])


@pytest.mark.spec("ARBORA-IDENTITY-R004.2")
async def test_indicators_appear_in_initialize_and_doctor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _mode(monkeypatch, "none")
    assert anonymous.indicator(config) == {
        "mode": "anonymous_local",
        "principal": "anonymous",
        "banner": BANNER,
        "doctor_check": "warn",
    }
    assert _with_unsecured_banner(SERVED_INSTRUCTIONS, config).startswith(BANNER)
    checks = await _doctor(config)
    assert checks["auth"] == anonymous.indicator(config)
    assert _check_auth(config)["status"] == "warn"

    config = _mode(monkeypatch, "none", exposed=True)
    assert anonymous.indicator(config)["doctor_check"] == "fail"
    assert (await _doctor(config))["auth"]["doctor_check"] == "fail"
    assert _check_auth(config)["status"] == "fail"

    config = _mode(monkeypatch, "external")
    assert anonymous.indicator(config) == {}
    assert _with_unsecured_banner(SERVED_INSTRUCTIONS, config) == SERVED_INSTRUCTIONS
    assert "auth" not in await _doctor(config)
    assert BANNER not in str(_check_auth(config))
