"""ARBORA-HOST-R007.3: gateway metric series use the ``arbora_`` prefix."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from arbora.observability import gateway_metrics
from arbora.observability.state import Metrics

_RETIRED = "meristem_"


def _declared_series() -> set[str]:
    source = Path(gateway_metrics.__file__).read_text(encoding="utf-8")
    declared = set(
        re.findall(
            r'^(?:\w+ = )?_(?:counter|gauge|histogram)\(\s*"([a-z_]+)"',
            source,
            re.M,
        )
    )
    declared |= set(re.findall(r'^\s+"((?:arbora|meristem)_[a-z_]+)",', source, re.M))
    return declared


@pytest.mark.spec("ARBORA-HOST-R007.3")
def test_no_series_uses_the_retired_prefix() -> None:
    declared = _declared_series()
    assert declared, "the census found no series declarations"
    assert not {name for name in declared if name.startswith(_RETIRED)}
    assert "arbora_gateway_requests_total" in declared
    assert "arbora_mcp_tool_calls_total" in declared


@pytest.mark.spec("ARBORA-HOST-R007.3")
def test_the_rendered_exposition_carries_no_retired_series() -> None:
    gateway_metrics.GATEWAY_REQUESTS.labels(
        route="/health", method="GET", status="200"
    ).inc()
    body, _ = gateway_metrics.render_metrics(Metrics())
    text = body.decode()
    assert "arbora_gateway_requests_total" in text
    assert _RETIRED not in text


@pytest.mark.asyncio
async def test_the_metrics_route_reads_the_host_on_the_application() -> None:
    from types import SimpleNamespace

    ran: list[str] = []
    metrics = Metrics()
    metrics.register(lambda: ran.append("sampled"))
    host = SimpleNamespace(observability=SimpleNamespace(metrics=metrics))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(host=host)))
    if not gateway_metrics.PROMETHEUS_AVAILABLE:
        pytest.skip("the metrics extra is not installed")
    response = await gateway_metrics.metrics_endpoint(request)
    assert response.status_code == 200
    assert ran == ["sampled"]
