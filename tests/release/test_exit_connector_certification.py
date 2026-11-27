"""Exit obligation ``connector_certification``: a certified connector onboards (ARBORA-RELEASE-R003.5).

The SDK conformance kit (``connector-certify``) certifies one fixture connector.
Only a connector that passed is onboarded through arbora; the engine then holds
its registration, attestation and imported pack. An uncertified connector is
refused before any onboarding runs.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import FastMCP
from mcp_types import ToolAnnotations
from scion_sdk.artifacts.pack import build_content_pack
from scion_sdk.certify.certification import CertificationReport, certify_connector
from scion_sdk.certify.read_only_check import ToolFixture
from scion_sdk.ingest.live_declaration import source_meta
from scion_sdk.manifest.live_contract import validate_live_tool_contract
from scion_sdk.mcp.declaration_resource import register_live_declaration
from scion_sdk.ports.session import TransportEndpoint
from scion_sdk.transports.mcp import McpTransport

from arbora.fleet import onboarding_capture as _onboarding_capture
from arbora.fleet.onboarding import onboard_fleet
from arbora.fleet.onboarding_capture import FleetEndpoint
from tests.fleet.test_fleet_onboarding import (  # noqa: F401
    _onboarding,
    _RecordingSink,
    _Registry,
    _run_in_session,
    eg,
)

CONNECTOR = FleetEndpoint("demo-mcp", "http://demo-mcp.example/mcp")


def _fixture_server() -> FastMCP:
    server: FastMCP = FastMCP("demo")

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True),
        meta=source_meta(
            source_id="items",
            record_type="Item",
            id_field="id",
            preset={"params_style": "args"},
        ),
    )
    def read_items() -> dict:
        return {"items": []}

    register_live_declaration(server, "demo")
    return server


async def _certify(server: FastMCP, *, proven: bool) -> CertificationReport:
    transport = McpTransport()
    endpoint = TransportEndpoint(in_process=server)
    fixtures: dict[str, ToolFixture] = {}
    if proven:
        async with transport.session(endpoint) as session:
            pin = validate_live_tool_contract(
                await session.list_tools(), tool_name="read_items"
            ).compatibility_sha256
        fixtures["read_items"] = ToolFixture(
            tool="read_items",
            fingerprint=pin,
            arguments={},
            observed_effects=frozenset(),
        )
    return await certify_connector(
        transport, endpoint, timeout_seconds=10, fixtures=fixtures
    )


def _capture_from(server: FastMCP) -> Any:
    async def capture(endpoint: FleetEndpoint, *, auth: Any = None) -> Any:
        async with McpTransport().session(TransportEndpoint(in_process=server)) as s:
            return await build_content_pack(
                s, connector=endpoint.name, kinds=_onboarding_capture._capture_kinds()
            )

    return capture


@pytest.mark.spec("ARBORA-RELEASE-R003.5")
async def test_certified_connector_onboards_and_the_engine_holds_its_pack(
    eg: _Registry,  # noqa: F811
) -> None:
    server = _fixture_server()
    # An uncertified connector is refused before any onboarding runs.
    unproven = await _certify(server, proven=False)
    assert not unproven.passed
    assert eg.registers == [] and _RecordingSink.imported == []

    report = await _certify(server, proven=True)
    assert report.passed
    onboarded = await onboard_fleet_async(eg, server)
    assert onboarded.failed == {}
    assert onboarded.onboarded == [CONNECTOR.name]
    # The engine holds the registration, the attestation and the imported pack.
    assert {params["name"] for params, _ in eg.registers} >= {CONNECTOR.name}
    assert eg.attested == [CONNECTOR.name]
    assert [name for name, _binding in _RecordingSink.imported] == [CONNECTOR.name]


async def onboard_fleet_async(eg: _Registry, server: FastMCP) -> Any:  # noqa: F811
    from arbora.security.session import use_session
    from tests.fleet.test_fleet_onboarding import SELF_URL, _session

    with use_session(_session()):
        return await onboard_fleet(
            _onboarding(eg, _capture_from(server)), (CONNECTOR,), served_url=SELF_URL
        )
