"""The unsecured-mode indicators (ARBORA-IDENTITY-R004.2).

The indicator reaches the MCP ``initialize`` instructions, the served ``ops.doctor``
report and the doctor auth check in none mode, and none of them outside it.
"""

from __future__ import annotations

from typing import Any

import pytest

from arbora.core.settings import runtime_config
from arbora.deployment.doctor import _check_auth
from arbora.mcp_server.runtime import SERVED_INSTRUCTIONS, _with_unsecured_banner
from arbora.security import anonymous
from tests.api.test_ops_admin_ops import _context, _FakeOpsAdminStore, ops_admin

_OPS = {op.id: op for op in ops_admin.operations()}
BANNER = "Arbora is running in unsecured loopback mode."


def _mode(monkeypatch: pytest.MonkeyPatch, mode: str, exposed: bool = False) -> None:
    monkeypatch.setattr(runtime_config, "arbora_auth_mode", mode)
    if exposed:
        monkeypatch.setenv("ARBORA_AUTH_NONE_EXPOSE", "acknowledged")
    else:
        monkeypatch.delenv("ARBORA_AUTH_NONE_EXPOSE", raising=False)


async def _doctor() -> dict[str, Any]:
    result = await ops_admin.handle_ops_doctor(
        _context(store=_FakeOpsAdminStore()), {}, _OPS["ops.doctor"]
    )
    return dict(result["value"]["checks"])


@pytest.mark.spec("ARBORA-IDENTITY-R004.2")
async def test_indicators_appear_in_initialize_and_doctor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mode(monkeypatch, "none")
    assert anonymous.indicator() == {
        "mode": "anonymous_local",
        "principal": "anonymous",
        "banner": BANNER,
        "doctor_check": "warn",
    }
    assert _with_unsecured_banner(SERVED_INSTRUCTIONS).startswith(BANNER)
    checks = await _doctor()
    assert checks["auth"] == anonymous.indicator()
    assert _check_auth()["status"] == "warn"

    _mode(monkeypatch, "none", exposed=True)
    assert anonymous.indicator()["doctor_check"] == "fail"
    assert (await _doctor())["auth"]["doctor_check"] == "fail"
    assert _check_auth()["status"] == "fail"

    _mode(monkeypatch, "external")
    assert anonymous.indicator() == {}
    assert _with_unsecured_banner(SERVED_INSTRUCTIONS) == SERVED_INSTRUCTIONS
    assert "auth" not in await _doctor()
    assert BANNER not in str(_check_auth())
