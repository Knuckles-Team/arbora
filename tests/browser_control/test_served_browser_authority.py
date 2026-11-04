"""Actual served-engine composition must use public caller-bound EG methods."""

import pytest

from arbora.browser_control.browser_control_service import (
    browser_control_factory_kwargs,
)
from arbora.mcp_server.served_engine import ServedEngine

pytestmark = pytest.mark.spec("ARBORA-FLEET-R015")


@pytest.fixture(autouse=True)
def isolated_webui_data(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))


def test_actual_served_engine_installs_browser_authority():
    from overstory.server import create_agent_web_app as factory

    async def sync_runner(call):
        return call()

    async def refuse(_binding):
        return False

    configured = browser_control_factory_kwargs(
        factory, ServedEngine(None), sync_runner, session_revalidator=refuse
    )
    assert configured["browser_control"] is not None


from tests.security.test_anonymous_mode import local_mode as local_mode
from tests.test_served_engine_native_labels import native_engine as native_engine


def test_native_browser_state_create_cas_and_labeled_read(native_engine):
    engine, _session = native_engine
    state = engine.browser_control_state
    properties = {
        "id": "browser-state",
        "node_type": "BrowserStateProbe",
        "status": "active",
    }
    assert state.create_node_if_absent("browser-state", properties=properties)
    assert not state.create_node_if_absent("browser-state", properties=properties)
    assert state.compare_and_set_node_fields(
        "browser-state", {"status": "active"}, {"status": "revoked"}
    )
    assert not state.compare_and_set_node_fields(
        "browser-state", {"status": "active"}, {"status": "consumed"}
    )
    assert state.query_cypher(
        "MATCH (n:BrowserStateProbe {id: $id}) RETURN n.status AS status",
        {"id": "browser-state"},
    ) == [{"status": "revoked"}]


def test_native_typed_browser_bundle_is_atomic_and_labeled(native_engine):
    engine, session = native_engine
    mutations = [
        {
            "kind": "node",
            "id": "trace-a",
            "node_type": "BrowserTraceProbe",
            "properties": {},
        },
        {
            "kind": "node",
            "id": "trace-b",
            "node_type": "BrowserTraceProbe",
            "properties": {},
        },
        {
            "kind": "edge",
            "source": "trace-a",
            "target": "trace-b",
            "rel_type": "USED_TOOL",
            "properties": {},
        },
    ]
    assert engine.batch_typed_mutations(mutations, session=session)
    assert engine.browser_control_state.query_cypher(
        "MATCH (a:BrowserTraceProbe)-[:USED_TOOL]->(b:BrowserTraceProbe) RETURN a.id AS source, b.id AS target"
    ) == [{"source": "trace-a", "target": "trace-b"}]


def test_browser_state_never_uses_process_session_fallback(native_engine, monkeypatch):
    from arbora.api.contracts.session import SessionRequiredError, suspend_session
    from arbora.mcp_server import bootstrap

    engine, session = native_engine
    monkeypatch.setattr(bootstrap, "_process_session", lambda: session)
    with suspend_session(), pytest.raises(SessionRequiredError):
        engine.browser_control_state.query_cypher("MATCH (n) RETURN n")


def test_browser_state_rechecks_authority_after_pool_await(native_engine, monkeypatch):
    from arbora.api.contracts.session import SessionExpiredError
    from arbora.security import anonymous

    engine, _session = native_engine
    connect = engine.pool.connected_client

    async def revoke_after_connect(*args):
        client = await connect(*args)
        monkeypatch.setattr(anonymous, "_ACTIVE_MODE", "required")
        return client

    monkeypatch.setattr(engine.pool, "connected_client", revoke_after_connect)
    with pytest.raises(SessionExpiredError):
        engine.browser_control_state.create_node_if_absent(
            "refused-browser-write",
            properties={"id": "refused-browser-write", "node_type": "BrowserRefused"},
        )
    monkeypatch.setattr(anonymous, "_ACTIVE_MODE", "none")
    monkeypatch.setattr(engine.pool, "connected_client", connect)
    assert (
        engine.browser_control_state.query_cypher("MATCH (n:BrowserRefused) RETURN n")
        == []
    )


def test_released_native_actual_webui_factory_composition(native_engine):
    import asyncio

    from overstory.browser_control import revalidate_browser_control_session
    from overstory.server import create_agent_web_app

    engine, _session = native_engine
    configured = browser_control_factory_kwargs(
        create_agent_web_app,
        engine,
        asyncio.to_thread,
        session_revalidator=revalidate_browser_control_session,
    )
    assert configured["browser_control"] is not None
    assert (
        configured["browser_control"]._authority.query_cypher(
            "MATCH (n:NoBrowserGrant) RETURN n"
        )
        == []
    )


def test_browser_state_requires_current_write_scope(native_engine):
    from dataclasses import replace

    from arbora.api.contracts.session import use_session

    engine, session = native_engine
    with (
        use_session(replace(session, scopes=frozenset({"kg:read"}))),
        pytest.raises(PermissionError),
    ):
        engine.browser_control_state.create_node_if_absent(
            "no-write-scope", properties={"node_type": "BrowserRefused"}
        )
    assert (
        engine.browser_control_state.query_cypher("MATCH (n:BrowserRefused) RETURN n")
        == []
    )


def test_browser_batch_refuses_foreign_session_graph(native_engine):
    from dataclasses import replace

    from arbora.api.contracts.session import SessionRequiredError

    engine, session = native_engine
    with pytest.raises(SessionRequiredError):
        engine.batch_typed_mutations([], session=replace(session, graph="foreign"))
