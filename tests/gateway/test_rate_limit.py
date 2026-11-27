"""ARBORA-HOST-R007.3: the rate-limit counter is an ``arbora_gateway_*`` series."""

from __future__ import annotations

import pytest

from arbora.gateway.rate_limit import GatewayRateLimitMiddleware
from arbora.observability.gateway_metrics import render_metrics
from arbora.observability.state import Metrics
from tests.api._support import make_verified_caller


async def _app(scope, receive, send) -> None:  # pragma: no cover - never reached
    raise AssertionError("the inner application is not called")


@pytest.mark.spec("ARBORA-HOST-R007.3")
def test_rate_limit_counter_renamed() -> None:
    limiter = GatewayRateLimitMiddleware(_app, rate=0.001, burst=1)
    caller = make_verified_caller("example:ask")()
    assert limiter.consume_verified_caller(caller)[0] is True
    assert limiter.consume_verified_caller(caller)[0] is False
    text = render_metrics(Metrics())[0].decode()
    assert "arbora_gateway_rate_limited_total" in text
    assert "meristem_gateway_rate_limited" not in text
