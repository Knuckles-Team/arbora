"""Real ASGI lifecycle with controlled owners; no native admission claim."""

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from arbora.agent_host import build_agent_host_app
from tests.agent_host.test_runtime_binding import ConstructionOwner


def application(owner):
    return build_agent_host_app(
        graph=owner.resources.graph,
        graph_config=owner.resources.graph_config,
        executor=owner.resources.executor,
        host_start=owner.context(),
        host_configuration=owner.configuration,
        host_runtime=owner.resources,
    )


@pytest.mark.spec("ARBORA-HOST-R010.7.2.2.2.1")
def test_lifespan_revalidates_and_preserves_borrowed_ownership(tmp_path: Path):
    owner = ConstructionOwner(tmp_path)
    engine, graph = Mock(), Mock()
    owner.resources = replace(owner.resources, engine=engine, graph=graph)
    owner.binding = replace(owner.binding, runtime=owner.resources)
    app = application(owner)
    assert owner.calls == 1
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert app.state.admitted_host_start.runtime is owner.resources
        assert owner.calls == 2
    assert not hasattr(app.state, "admitted_host_start")
    engine.close.assert_not_called()
    graph.close.assert_not_called()


@pytest.mark.spec("ARBORA-HOST-R010.7.2.2.2.1")
@pytest.mark.parametrize(
    "change", ["expiry", "revoke", "revision", "resource", "shortened"]
)
def test_changed_start_cannot_enter_lifespan(tmp_path: Path, change: str):
    owner = ConstructionOwner(tmp_path)
    app = application(owner)
    if change == "expiry":
        owner.state = replace(owner.state, expires_at_ms=int(time.time() * 1000) - 1)
    elif change == "revoke":
        owner.revoke = True
    elif change == "shortened":
        owner.binding = replace(
            owner.binding, expires_at_ms=owner.state.expires_at_ms - 1000
        )
    elif change == "revision":
        owner.binding = replace(owner.binding, configuration_revision="changed")
    else:
        owner.binding = replace(
            owner.binding, runtime=replace(owner.resources, engine=object())
        )
    with pytest.raises(PermissionError):
        with TestClient(app):
            pytest.fail("changed authority reached readiness")
    assert not hasattr(app.state, "admitted_host_start")


@pytest.mark.spec("ARBORA-HOST-R010.7.2.2.2.1")
def test_partial_runtime_admission_is_unavailable(tmp_path: Path):
    owner = ConstructionOwner(tmp_path)
    with pytest.raises(PermissionError, match="configuration_unavailable"):
        build_agent_host_app(
            graph=object(), graph_config={}, host_start=owner.context()
        )


@pytest.mark.spec("ARBORA-HOST-R010.7.2.2.2.1")
def test_cancelled_lifespan_clears_readiness(tmp_path: Path):
    owner = ConstructionOwner(tmp_path)
    app = application(owner)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            async with app.router.lifespan_context(app):
                assert app.state.admitted_host_start.runtime is owner.resources
                raise asyncio.CancelledError
        assert not hasattr(app.state, "admitted_host_start")

    asyncio.run(run())


@pytest.mark.spec("ARBORA-HOST-R010.7.2.2.2.1")
def test_app_cannot_omit_the_admitted_graph(tmp_path: Path):
    owner = ConstructionOwner(tmp_path)
    with pytest.raises(PermissionError, match="configuration_unavailable"):
        build_agent_host_app(
            graph=None,
            graph_config=owner.resources.graph_config,
            host_start=owner.context(),
            host_configuration=owner.configuration,
            host_runtime=owner.resources,
        )


@pytest.mark.spec("ARBORA-HOST-R010.7.2.2.2.1")
def test_falsey_admitted_executor_is_preserved(tmp_path: Path):
    class Executor:
        def __bool__(self):
            return False

        def __call__(self, graph, config, query):
            assert graph is owner.resources.graph
            assert config is owner.resources.graph_config
            assert query == "hello"
            return {"output": "borrowed executor"}

    owner = ConstructionOwner(tmp_path)
    owner.resources = replace(owner.resources, executor=Executor())
    owner.binding = replace(owner.binding, runtime=owner.resources)
    with TestClient(application(owner)) as client:
        response = client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "test",
                "method": "message/send",
                "params": {"message": {"parts": [{"kind": "text", "text": "hello"}]}},
            },
        )
        assert response.status_code == 200
        assert "error" not in response.json()
        assert "borrowed executor" in response.text
