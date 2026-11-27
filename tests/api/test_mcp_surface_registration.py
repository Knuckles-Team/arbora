"""The six intent verbs register from ``mcp_server`` (ARBORA-HOST-R038.5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from arbora.mcp_server.registration import register_mcp_tools
from tests.api.test_mcp_shared_path import surface  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[2] / "arbora"
VERBS = {"ask", "find", "why", "write", "act", "manage"}


@pytest.mark.spec("ARBORA-HOST-R038.5")
def test_six_verbs_register_from_mcp_server(surface) -> None:  # noqa: F811
    async def run() -> set[str]:
        await register_mcp_tools(surface.mcp, surface.mux, surface.binding)
        return {tool.name for tool in await surface.mcp.list_tools()}

    assert VERBS <= asyncio.run(run())
    assert register_mcp_tools.__module__ == "arbora.mcp_server.registration"


@pytest.mark.spec("ARBORA-HOST-R038.5")
def test_composition_modules_left_api_and_identity_without_a_re_export() -> None:
    for old in (
        "api/mcp/registration.py",
        "api/mcp/serving.py",
        "api/http/browser_app.py",
        "identity/browser_composition.py",
    ):
        assert not (ROOT / old).exists(), old
    for new in (
        "registration.py",
        "serving.py",
        "browser_app.py",
        "browser_composition.py",
    ):
        assert (ROOT / "mcp_server" / new).is_file(), new
