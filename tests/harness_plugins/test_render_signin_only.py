"""The bundle names the door and the sign-in path, never a connector setting (R011.2)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from arbora.harness_plugins.compose import compose_bundle
from arbora.harness_plugins.profiles import profile
from arbora.harness_plugins.render import render
from tests.harness_plugins.conftest import ENDPOINT

_URL = re.compile(r"https?://[^\s\"'<>)\]]+")
_CALLBACK = re.compile(r"http://127\.0\.0\.1:\d+/mcp/oauth/callback$")
_CONNECTOR_SETTING = re.compile(
    r"(client_secret|api[_-]?key|bearer|authorization|password|connector_url)", re.I
)


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R011.2")
@pytest.mark.parametrize("profile_id", ["claude-code", "opencode"])
def test_bundle_holds_door_endpoint_and_no_connector_settings(
    skill_root: Path, tmp_path: Path, profile_id: str
) -> None:
    tree = tmp_path / profile_id
    render(
        compose_bundle(
            profile(profile_id), base_url=ENDPOINT, skill_roots=[skill_root]
        ),
        tree,
    )
    files = [path for path in sorted(tree.rglob("*")) if path.is_file()]
    text = "\n".join(path.read_text(errors="replace") for path in files)
    # Every URL is the one door endpoint or the loopback sign-in callback.
    urls = {url.rstrip(".,") for url in _URL.findall(text)}
    assert any(url.startswith(ENDPOINT) for url in urls), urls
    assert all(url.startswith(ENDPOINT) or _CALLBACK.match(url) for url in urls), urls
    # No token, secret or connector setting travels in the bundle.
    assert _CONNECTOR_SETTING.search(text) is None
    # Exactly one MCP server entry: the door, no per-connector second connection.
    servers = 0
    for path in files:
        if path.suffix == ".json":
            document = json.loads(path.read_text())
            if not isinstance(document, dict):
                continue
            # claude-code names servers under mcpServers; opencode names one door.
            servers += len(document.get("mcpServers") or {})
            servers += 1 if isinstance(document.get("door"), dict) else 0
    assert servers == 1
