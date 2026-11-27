"""The served agent surface follows the deployed release and the reconciliation gate (ARBORA-DEPLOY-POLICY-R006)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client, FastMCP

from arbora.api.domain.fleet.mcp_resource_reconciliation import (
    MCP_RESOURCE_FAMILIES,
    PublicationRefusedError,
    ReconciliationReceipt,
)
from arbora.api.ops import policy
from arbora.api.registry import Registry
from arbora.fleet.agent_surface import (
    AgentSurface,
    SurfaceCandidate,
    export_skills,
    register_agent_surface,
)

RELEASE = "sha256:" + "c" * 64
CANDIDATE = SurfaceCandidate("tenant-a", 7, "sha256:" + "d" * 64)


def _receipt(**over: Any) -> ReconciliationReceipt:
    fields: dict[str, Any] = {
        "tenant_id": "tenant-a",
        "catalog_generation": 7,
        "snapshot_digest": CANDIDATE.snapshot_digest,
        "acknowledged_families": MCP_RESOURCE_FAMILIES,
        "issued_at_ms": 1_000,
    }
    return ReconciliationReceipt(**{**fields, **over})


def _surface(
    receipt: ReconciliationReceipt | None, release: str = RELEASE
) -> AgentSurface:
    async def source() -> ReconciliationReceipt | None:
        return receipt

    return AgentSurface(
        registry=Registry(list(policy.operations())),
        release_digest=release,
        candidate=CANDIDATE,
        receipt=source,
        clock_ms=lambda: 2_000,
    )


def _get(mcp: FastMCP, path: str) -> httpx.Response:
    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=mcp.http_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as http:
            return await http.get(path)

    return asyncio.run(call())


def _resource(mcp: FastMCP, uri: str) -> str:
    async def read() -> str:
        async with Client(mcp) as client:
            return (await client.read_resource(uri))[0].text

    return asyncio.run(read())


# spec: ARBORA-DEPLOY-POLICY-R006
def test_llms_txt_and_resources_match_the_deployed_digest(tmp_path: Path) -> None:
    surface = _surface(_receipt())
    mcp = FastMCP("surface")
    register_agent_surface(mcp, surface)

    page = _get(mcp, "/llms.txt")
    assert page.status_code == 200
    assert f"Release digest: {RELEASE}" in page.text
    assert "- policy.read:" in page.text

    text = _resource(mcp, "arbora://best-practices/discover-first")
    assert f"release-digest: {RELEASE}" in text

    # A new deployed digest regenerates the artifacts under the new digest.
    newer = "sha256:" + "e" * 64
    other = FastMCP("surface-2")
    register_agent_surface(other, _surface(_receipt(), newer))
    assert f"Release digest: {newer}" in _get(other, "/llms.txt").text

    skills = tmp_path / "skills"
    (skills / "demo").mkdir(parents=True)
    (skills / "demo" / "SKILL.md").write_text("# demo\n")
    out = tmp_path / "out"
    written = asyncio.run(export_skills(surface, skills, out, digest_stamp=True))
    assert written == ["demo"]
    assert (
        (out / "demo" / "SKILL.md")
        .read_text()
        .startswith(f"<!-- release-digest: {RELEASE} -->")
    )


# spec: ARBORA-DEPLOY-POLICY-R006
def test_stale_or_missing_receipt_refuses_generation(tmp_path: Path) -> None:
    for receipt, reason in (
        (None, "missing"),
        (_receipt(issued_at_ms=-10_000_000), "stale"),
        (_receipt(catalog_generation=6), "invalid"),
    ):
        surface = _surface(receipt)
        mcp = FastMCP("surface")
        register_agent_surface(mcp, surface)
        refused = _get(mcp, "/llms.txt")
        assert refused.status_code == 503
        assert refused.json() == {"code": "reingestion-unreconciled", "reason": reason}
        with pytest.raises(Exception, match="reingestion-unreconciled"):
            _resource(mcp, "arbora://best-practices/discover-first")

        skills = tmp_path / reason
        (skills / "demo").mkdir(parents=True)
        (skills / "demo" / "SKILL.md").write_text("# demo\n")
        with pytest.raises(PublicationRefusedError):
            asyncio.run(
                export_skills(surface, skills, tmp_path / "x", digest_stamp=True)
            )
        assert not (tmp_path / "x").exists()
