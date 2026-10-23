"""Public behaviour of the messaging command registry and Telegram rendering."""

from __future__ import annotations

import json

import pytest

from arbora.messaging.commands import (
    COMMANDS,
    command_specs,
    describe,
    handle_command,
)
from arbora.messaging.render import markdown_to_telegram_html as render

pytestmark = pytest.mark.spec("ARBORA-MESSAGING-R800")


class _Svc:
    def status(self):
        return {"configured": ["telegram"], "connected": ["telegram"]}


class _KgSvc:
    class _Eng:
        def query_cypher(self, q, _p):
            if "count" in q:
                return [{"c": 12 if "Server" in q else 40}]
            return [{"name": "gitlab-mcp"}]

    def _resolve_engine(self):
        return self._Eng()


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("**b** and *i* and `c`", "<b>b</b> and <i>i</i> and <code>c</code>"),
        ("a < b & c > d", "a &lt; b &amp; c &gt; d"),
        ("see [r](https://e.com/x?a=1)", 'see <a href="https://e.com/x?a=1">r</a>'),
        ("", ""),
    ],
)
def test_render_inline_forms(markdown, expected):
    assert render(markdown) == expected


def test_render_structure_leaks_no_markdown_markers():
    out = render("## Stacks\n- `arbora` - up\n- *db*\n```python\n1 < 2\n```")
    assert "<b>Stacks</b>" in out
    assert "\u2022 <code>arbora</code> - up" in out
    assert "<pre>" in out and "1 &lt; 2" in out
    assert not any(m in out for m in ("##", "\n- ", "```"))


# spec: ARBORA-MESSAGING-R800.4
def test_command_specs_match_registry_and_filter_by_surface():
    assert {s["command"] for s in command_specs()} == {c.name for c in COMMANDS}
    messaging = {s["command"] for s in command_specs("messaging")}
    assert "model" in {s["command"] for s in command_specs("terminal")}
    assert "model" not in messaging
    assert {"help", "status", "tools"} <= messaging
    assert describe("/goal") == describe("goal") and describe("nope") is None


# spec: ARBORA-MESSAGING-R800.2
@pytest.mark.asyncio
async def test_builtin_commands_answer_locally():
    help_reply = await handle_command("/help@SomeBot", service=_Svc())
    assert all(f"/{c.name}" in help_reply for c in COMMANDS)
    status = await handle_command("/status", service=_Svc())
    assert json.loads(status.split("status: ", 1)[1])["connected"] == ["telegram"]
    tools = await handle_command("/tools", service=_KgSvc())
    assert "12 MCP servers" in tools and "40 skills" in tools
    assert "on demand" in await handle_command("/tools", service=_Svc())


# spec: ARBORA-MESSAGING-R800.2, ARBORA-MESSAGING-R800.4
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["hello", "/claude do x", "/skill foo", "/unknown", ""]
)
async def test_non_builtin_text_falls_through_to_the_agent(text):
    assert await handle_command(text, service=_Svc()) is None
