"""The rendered sandbox runs Arbora and the web UI in no-auth mode on loopback (R019.1)."""

from __future__ import annotations

import pytest

from arbora.deployment import sandbox
from arbora.identity.sandbox_principal import SandboxPrincipalRefusal


@pytest.mark.spec("ARBORA-DEPLOY-R019.1")
def test_render_sets_both_no_auth_modes_and_the_web_ui_co_service() -> None:
    rendered = sandbox.render_sandbox(engine_port=19100, webui_port=18080)

    env = rendered["environment"]
    assert env["ARBORA_AUTH_MODE"] == sandbox.NO_AUTH_MODE == "none"
    assert env["WEBUI_AUTH_MODE"] == "none"
    assert env["ENABLE_WEB_UI"] == "true"
    assert env["ARBORA_WEBUI_PORT"] == "18080"
    assert rendered["co_services"] == ["webui"]
    assert all(
        address.startswith("127.0.0.1:") for address in rendered["listeners"].values()
    )


@pytest.mark.spec("ARBORA-DEPLOY-R019.1")
@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "localhost", "::"])
def test_render_refuses_anything_but_loopback(host: str) -> None:
    with pytest.raises(SandboxPrincipalRefusal) as refused:
        sandbox.render_sandbox(engine_port=19100, bind_host=host)
    assert refused.value.code == "SANDBOX_PRINCIPAL_NOT_LOOPBACK"
