from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.fleet.child_resilience import ChildRuntime
from tests.fleet.catalog_fixture import multiplexer_from_fixture

pytestmark = pytest.mark.spec("GRAPHOS-FLEET-R035")

OPERATION = "get_market"
ALIAS = "operation_get_market_0123456789abcdef"
PREFIXED = f"markets__{ALIAS}"


class SchemaSession:
    def __init__(self, resources: dict[str, str], descriptor: Any) -> None:
        self.resources = resources
        self.descriptor = descriptor
        self.events: list[tuple[str, str]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.on_read = None

    async def list_tools(self) -> Any:
        return SimpleNamespace(tools=[self.descriptor])

    async def read_resource(self, uri: Any) -> Any:
        resource_uri = str(uri)
        self.events.append(("resource", resource_uri))
        if self.on_read is not None:
            self.on_read(resource_uri)
        return SimpleNamespace(
            contents=[
                SimpleNamespace(
                    mime_type="application/json",
                    text=self.resources[resource_uri],
                )
            ]
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.events.append(("call", name))
        self.calls.append((name, arguments))
        return SimpleNamespace(is_error=False, content=[])


def disclosure_fixture(
    alias: str = ALIAS,
) -> tuple[Any, Any, dict[str, Any], dict[str, str]]:
    from agent_connector_sdk.mcp.schema_disclosure import (
        build_schema_disclosure,
        read_schema_fragment,
    )

    input_schema = {
        "oneOf": [
            {
                "type": "object",
                "properties": {"action": {"enum": [OPERATION]}},
                "required": ["action"],
            }
        ]
    }
    output_schema = {"type": "object", "properties": {"market": {"type": "string"}}}
    disclosure = build_schema_disclosure(
        (input_schema, output_schema),
        operation=OPERATION,
        alias=alias,
        source_sha256="a" * 64,
    )
    manifest = disclosure.manifest
    resources = {
        manifest.manifest_uri: manifest.model_dump_json(),
        **{
            manifest.fragment_uri_template.format(index=index): json.dumps(
                read_schema_fragment(
                    disclosure,
                    index,
                    expected_sha256=manifest.schema_sha256,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for index in range(manifest.fragment_count)
        },
    }
    descriptor = SimpleNamespace(
        name=PREFIXED,
        meta={"openapi_schema": manifest.model_dump()},
        input_schema=input_schema,
        output_schema=output_schema,
        annotations={"readOnlyHint": True},
    )
    return manifest, descriptor, {"action": OPERATION}, resources


def setup_mux(tmp_path: Any, runtime: ChildRuntime, descriptor: Any) -> Any:
    mux = multiplexer_from_fixture(tmp_path / "catalog.json")
    mux._catalog = {"markets": {}}
    mux.children["markets"] = runtime
    mux.tool_to_server[PREFIXED] = ("markets", ALIAS)
    mux.aggregated_tools.append(descriptor)
    mux._global_visible.add(PREFIXED)
    return mux


def connect_schema_session(tmp_path: Any, session: SchemaSession) -> Any:
    """Attach the supplied schema session through the ordinary child runtime."""
    runtime = ChildRuntime("markets", {})
    runtime.adopt_sessions([session])
    return setup_mux(tmp_path, runtime, session.descriptor)


@pytest.mark.usefixtures("stdio_fleet_authority")
async def test_proxied_openapi_alias_reconstructs_before_call(tmp_path):
    _manifest, descriptor, arguments, resources = disclosure_fixture()
    session = SchemaSession(resources, descriptor)
    mux = connect_schema_session(tmp_path, session)

    await mux.call_proxied_tool(PREFIXED, arguments)

    assert session.events[0] == ("resource", _manifest.manifest_uri)
    assert [kind for kind, _ in session.events[:-1]] == ["resource"] * (
        1 + _manifest.fragment_count
    )
    assert session.events[-1] == ("call", ALIAS)
    assert session.calls == [(ALIAS, arguments)]


@pytest.mark.usefixtures("stdio_fleet_authority")
async def test_stale_manifest_never_calls_alias(tmp_path):
    manifest, descriptor, arguments, resources = disclosure_fixture()
    stale_resources = dict(resources)
    stale = manifest.model_dump()
    stale["source_sha256"] = "b" * 64
    stale_resources[manifest.manifest_uri] = json.dumps(stale)
    session = SchemaSession(stale_resources, descriptor)
    mux = connect_schema_session(tmp_path, session)

    result = await mux.call_proxied_tool(PREFIXED, arguments)

    assert result.is_error
    assert session.calls == []
    assert len(session.events) == 1


@pytest.mark.usefixtures("stdio_fleet_authority")
async def test_missing_fragment_never_calls_alias(tmp_path):
    manifest, descriptor, arguments, resources = disclosure_fixture()
    incomplete = dict(resources)
    incomplete.pop(manifest.fragment_uri_template.format(index=0))
    session = SchemaSession(incomplete, descriptor)
    mux = connect_schema_session(tmp_path, session)

    result = await mux.call_proxied_tool(PREFIXED, arguments)

    assert result.is_error
    assert session.calls == []
    assert session.events == [
        ("resource", manifest.manifest_uri),
        ("resource", manifest.fragment_uri_template.format(index=0)),
    ]


@pytest.mark.usefixtures("stdio_fleet_authority")
async def test_missing_admitted_output_schema_never_calls_alias(tmp_path):
    manifest, descriptor, arguments, resources = disclosure_fixture()
    descriptor.output_schema = None
    session = SchemaSession(resources, descriptor)
    mux = connect_schema_session(tmp_path, session)

    result = await mux.call_proxied_tool(PREFIXED, arguments)

    assert result.is_error
    assert session.calls == []
    assert session.events == [
        ("resource", manifest.manifest_uri),
        *[
            (
                "resource",
                manifest.fragment_uri_template.format(index=index),
            )
            for index in range(manifest.fragment_count)
        ],
    ]


@pytest.mark.usefixtures("stdio_fleet_authority")
async def test_mismatched_action_never_calls_alias(tmp_path):
    _manifest, descriptor, _arguments, resources = disclosure_fixture()
    session = SchemaSession(resources, descriptor)
    mux = connect_schema_session(tmp_path, session)

    result = await mux.call_proxied_tool(PREFIXED, {"action": "list_events"})

    assert result.is_error
    assert session.calls == []
    assert session.events[-1][0] == "resource"


@pytest.mark.usefixtures("stdio_fleet_authority")
async def test_catalog_change_during_fragment_reads_stops_before_alias_call(tmp_path):
    manifest, descriptor, arguments, resources = disclosure_fixture()
    session = SchemaSession(resources, descriptor)
    mux = connect_schema_session(tmp_path, session)
    changed = False

    def change_catalog(uri: str) -> None:
        nonlocal changed
        if not changed:
            changed = True
            mux._catalog_epoch += 1

    session.on_read = change_catalog
    result = await mux.call_proxied_tool(PREFIXED, arguments)

    assert result.is_error
    assert session.calls == []
    assert session.events == [("resource", manifest.manifest_uri)]


@pytest.mark.usefixtures("stdio_fleet_authority")
async def test_denied_session_reads_no_schema_resource(tmp_path):
    _manifest, descriptor, arguments, resources = disclosure_fixture()
    session = SchemaSession(resources, descriptor)
    mux = connect_schema_session(tmp_path, session)
    mux._global_visible.clear()

    result = await mux.call_proxied_tool(PREFIXED, arguments)

    assert result.is_error
    assert session.events == []
    assert session.calls == []


@pytest.mark.usefixtures("stdio_fleet_authority")
async def test_uncertain_write_alias_is_not_replayed(tmp_path):
    _manifest, descriptor, arguments, resources = disclosure_fixture()
    # Operation aliases clone domain annotations, which may not distinguish
    # a mutating action. A disclosure-bound operation is therefore never replayed.
    descriptor.annotations = {"readOnlyHint": True}

    class FailedWriteSession(SchemaSession):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            self.events.append(("call", name))
            self.calls.append((name, arguments))
            raise ConnectionResetError("write outcome is uncertain")

    session = FailedWriteSession(resources, descriptor)
    mux = connect_schema_session(tmp_path, session)

    result = await mux.call_proxied_tool(PREFIXED, arguments)

    assert result.is_error
    assert session.calls == [(ALIAS, arguments)]


@pytest.mark.usefixtures("stdio_fleet_authority")
async def test_oauth_preflight_and_alias_call_share_each_ephemeral_session(
    tmp_path, monkeypatch
):
    manifest, descriptor, arguments, resources = disclosure_fixture()
    oauth_descriptor = SimpleNamespace(
        name=ALIAS,
        meta=descriptor.meta,
        input_schema=descriptor.input_schema,
        output_schema=descriptor.output_schema,
        annotations=descriptor.annotations,
    )
    sessions = [
        SchemaSession(resources, oauth_descriptor),
        SchemaSession(resources, oauth_descriptor),
    ]
    mux, cfg, opened = setup_oauth_sessions(tmp_path, monkeypatch, sessions)
    for _ in range(2):
        await mux._call_ephemeral_oauth_session(
            "markets", ALIAS, cfg, arguments, mux._catalog_epoch
        )

    assert len(opened) == 2 and opened[0] is not opened[1]
    for session in opened:
        assert session.events[0] == ("resource", manifest.manifest_uri)
        assert session.events[-1] == ("call", ALIAS)


def setup_oauth_sessions(
    tmp_path: Any, monkeypatch: Any, sessions: list[SchemaSession]
):
    """Open scripted sessions through the real ephemeral OAuth call path."""
    from arbora.fleet import multiplexer as multiplexer_module

    cfg: dict[str, Any] = {}
    mux = multiplexer_from_fixture(tmp_path / "catalog.json")
    mux._catalog = {"markets": cfg}
    mux._catalog_epoch = 4
    monkeypatch.setattr(
        multiplexer_module,
        "_prepare_runtime_child_policy",
        lambda current: (current, None),
    )
    opened: list[SchemaSession] = []

    async def open_one(_server: str, _cfg: dict[str, Any], _stack: Any) -> Any:
        session = sessions[len(opened)]
        opened.append(session)
        return session

    monkeypatch.setattr(mux, "_open_one_session", open_one)
    return mux, cfg, opened


@pytest.mark.usefixtures("stdio_fleet_authority")
async def test_nonprefixed_oauth_alias_refuses_stale_schema(tmp_path, monkeypatch):
    alias = "read_entry"
    manifest, descriptor, arguments, resources = disclosure_fixture(alias)
    descriptor.name = alias
    stale = manifest.model_dump()
    stale["source_sha256"] = "b" * 64
    resources[manifest.manifest_uri] = json.dumps(stale)
    session = SchemaSession(resources, descriptor)
    mux, cfg, opened = setup_oauth_sessions(tmp_path, monkeypatch, [session])

    with pytest.raises(Exception, match="no longer matches"):
        await mux._call_ephemeral_oauth_session(
            "markets", alias, cfg, arguments, mux._catalog_epoch
        )

    assert opened == [session]
    assert session.events == [("resource", manifest.manifest_uri)]
    assert session.calls == []


@pytest.mark.usefixtures("stdio_fleet_authority")
async def test_oauth_ordinary_tool_calls_without_schema_resources(
    tmp_path, monkeypatch
):
    descriptor = SimpleNamespace(name="read_entry", meta={})
    session = SchemaSession({}, descriptor)
    mux, cfg, _opened = setup_oauth_sessions(tmp_path, monkeypatch, [session])

    await mux._call_ephemeral_oauth_session(
        "markets", descriptor.name, cfg, {}, mux._catalog_epoch
    )

    assert session.events == [("call", descriptor.name)]
    assert session.calls == [(descriptor.name, {})]
