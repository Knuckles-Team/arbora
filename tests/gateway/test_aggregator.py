"""The dashboard aggregator reads widgets through ``ecosystem.widgets.read``.

There is no widget registry and no gateway widget module; one path serves every
widget (ARBORA-OPS-R914).
"""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from typing import Any

import pytest

from arbora.gateway.aggregator import WIDGET_READ_OP, Aggregator, widget_data
from arbora.gateway.config import ConfigManager
from arbora.gateway.models import (
    DashboardLayout,
    ServiceCategory,
    ServiceConfig,
    ServiceGroup,
)


def _manager(tmp_path: Any, *widget_types: str) -> ConfigManager:
    manager = ConfigManager(config_path=tmp_path / "services.yaml")
    services = [
        ServiceConfig(
            id=f"{kind}-test",
            name=kind,
            widget_type=kind,
            category=ServiceCategory.INFRASTRUCTURE,
        )
        for kind in widget_types
    ]
    manager.save(DashboardLayout(groups=[ServiceGroup(name="g", services=services)]))
    return manager


class _Reads:
    def __init__(self, replies: Mapping[str, Any]) -> None:
        self.replies = replies
        self.widgets: list[str] = []

    async def __call__(self, widget: str) -> Any:
        self.widgets.append(widget)
        reply = self.replies[widget]
        if isinstance(reply, BaseException):
            raise reply
        return reply


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-OPS-R914")
async def test_each_service_is_read_through_the_one_served_operation(tmp_path) -> None:
    read = _Reads(
        {
            "portainer": {
                "rows": [{"key": "running", "label": "running", "value": 4}],
                "source": "portainer",
                "degraded": False,
                "fetched_at": "t",
            },
            "arr": {
                "rows": [{"title": "one"}],
                "source": "arr",
                "degraded": True,
                "fetched_at": "t",
            },
        }
    )
    agg = Aggregator(config_manager=_manager(tmp_path, "portainer", "arr"))

    results = await agg.fetch_all(read)

    assert sorted(read.widgets) == ["arr", "portainer"]
    assert results["portainer-test"].status == "ok"
    assert results["portainer-test"].fields == {"running": 4}
    assert results["arr-test"].status == "degraded"
    assert results["arr-test"].fields == {"rows": [{"title": "one"}]}
    assert WIDGET_READ_OP == "ecosystem.widgets.read"


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-OPS-R914")
async def test_a_failed_read_is_one_error_widget_and_an_unknown_id_is_absent(
    tmp_path,
) -> None:
    read = _Reads({"portainer": RuntimeError("secret detail")})
    agg = Aggregator(config_manager=_manager(tmp_path, "portainer"))

    failed = await agg.fetch_one("portainer-test", read)
    missing = await agg.fetch_one("nope", read)

    assert failed.status == "error"
    assert "secret detail" not in (failed.error or "")
    assert failed.raw is not None and "correlation_id" in failed.raw
    assert missing.status == "error" and "not found" in (missing.error or "")
    assert read.widgets == ["portainer"]


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-OPS-R914")
async def test_health_is_the_read_not_degrading(tmp_path) -> None:
    ok = {"rows": [], "source": "a", "degraded": False, "fetched_at": "t"}
    read = _Reads({"portainer": ok, "arr": {**ok, "degraded": True}})
    agg = Aggregator(config_manager=_manager(tmp_path, "portainer", "arr"))

    assert await agg.health_check(read) == {"portainer-test": True, "arr-test": False}


@pytest.mark.spec("ARBORA-OPS-R914")
def test_the_gateway_widget_package_and_registry_are_gone() -> None:
    assert importlib.util.find_spec("arbora.gateway.widgets") is None
    assert importlib.util.find_spec("arbora.gateway.registry") is None
    reply = {"rows": [], "source": "x", "degraded": False, "fetched_at": "t"}
    assert widget_data(reply).fields == {"rows": []}
