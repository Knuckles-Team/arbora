"""arbora serves while the catalog warms (ARBORA-FLEET-R078)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.mcp_server.catalog_warmup import CatalogWarmup


@pytest.mark.spec("ARBORA-FLEET-R078")
def test_an_engine_timeout_is_retried_and_never_escapes() -> None:
    calls: list[int] = []
    after: list[str] = []

    async def compose() -> None:
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("epistemic-graph RPC 'ConnectorPack' timed out")

    warmup = CatalogWarmup(
        compose, after=lambda: after.append("onboard"), retry_base_s=0.0
    )
    warmup.run()
    state = warmup.state()
    assert state["status"] == "ready"
    assert state["attempts"] == 3
    assert state["last_error"] is None
    assert after == ["onboard"]


@pytest.mark.spec("ARBORA-FLEET-R078")
def test_a_retrying_warmup_reports_its_typed_state_and_stops() -> None:
    async def compose() -> None:
        raise TimeoutError("engine busy")

    warmup = CatalogWarmup(compose, retry_base_s=30.0)
    thread = warmup.start()
    deadline = time.monotonic() + 5
    while warmup.state()["status"] != "retrying" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert warmup.state()["status"] == "retrying"
    assert warmup.state()["last_error"] == "TimeoutError"
    warmup.stop()
    thread.join(timeout=5)
    assert warmup.state()["status"] == "stopped"


@pytest.mark.spec("ARBORA-FLEET-R078")
def test_a_catalog_read_before_install_is_a_retryable_refusal() -> None:
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.fleet.catalog_reader import DeferredFleetCatalogReader

    reader = DeferredFleetCatalogReader()
    assert reader.installed is False
    with pytest.raises(OperationRefused) as caught:
        asyncio.run(reader.read())
    assert caught.value.code == "UNAVAILABLE"
    assert caught.value.details["reason"] == "catalog warming"
    assert caught.value.details["retry_after_s"] > 0


@pytest.mark.spec("ARBORA-FLEET-R078")
def test_boot_returns_while_the_catalog_composition_is_still_running(
    monkeypatch,
) -> None:
    import arbora.api.contracts.session as session_module
    import arbora.mcp_server.catalog_composition as composition
    import arbora.security.actor_context as actor_module
    from arbora.mcp_server import catalog_warmup, server

    async def slow_compose(**_kwargs: Any) -> None:
        await asyncio.sleep(2.0)

    monkeypatch.setattr(composition, "compose_catalog_authorities", slow_compose)
    monkeypatch.setattr(actor_module, "use_actor", lambda _a: contextlib.nullcontext())
    monkeypatch.setattr(
        session_module, "use_session", lambda _s: contextlib.nullcontext()
    )
    began = time.monotonic()
    warmup = server._start_catalog_warmup(
        object(), SimpleNamespace(actor=None), object(), object()
    )
    assert time.monotonic() - began < 1.0
    state = catalog_warmup.catalog_state()
    assert state is not None
    assert state["status"] == "warming"
    warmup.stop()
