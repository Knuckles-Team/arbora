"""Each runtime configuration view owns its overrides (ARBORA-HOST-R039.11)."""

from __future__ import annotations

import pytest


@pytest.mark.spec("ARBORA-HOST-R039.11")
def test_two_runtime_configs_do_not_share_state():
    from arbora.core.settings import RuntimeConfig
    from arbora.mcp_server.host_context import build_host_context

    first, second = RuntimeConfig(), RuntimeConfig()
    first.gateway_rate_limit = 7.0
    assert first.gateway_rate_limit == 7.0
    assert second.gateway_rate_limit != 7.0
    del first.gateway_rate_limit
    assert first.gateway_rate_limit == second.gateway_rate_limit
    one, other = build_host_context(), build_host_context()
    assert isinstance(one.config, RuntimeConfig)
    assert one.config is not other.config
