"""The deployment page documents the sandbox as the code renders it (ARBORA-DEPLOY-R019.1)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from arbora.deployment import sandbox

_PAGE = Path(__file__).resolve().parents[2] / "docs" / "deployment.md"


def _local_section() -> str:
    text = _PAGE.read_text(encoding="utf-8")
    start = text.index("## Local sandbox")
    return text[start : text.index("\n## ", start + 1)]


@pytest.mark.spec("ARBORA-DEPLOY-R019.1")
def test_documented_sandbox_commands_and_variables_match_the_code() -> None:
    section = _local_section()
    rendered = sandbox.render_sandbox(engine_port=19100)

    assigned = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})=", section))
    assert {"ARBORA_AUTH_MODE", "WEBUI_AUTH_MODE"} <= assigned
    for name in assigned:
        assert name in rendered["environment"], name
    for name in rendered["environment"]:
        assert f"`{name}" in section, name

    for command in re.findall(r"`arbora-sandbox (\w+)`", section):
        assert command in {"up", "down"}
    assert "`arbora-sandbox up`" in section and "`arbora-sandbox down`" in section

    parser_text = Path(sandbox.__file__).read_text(encoding="utf-8")
    for flag in re.findall(r"`(--[a-z-]+)`", section):
        assert f'"{flag}"' in parser_text, flag
    assert str(sandbox.DEFAULT_WEBUI_PORT) in section
    assert "3 runs per hour" in section
