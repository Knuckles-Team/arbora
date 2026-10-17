"""One fleet tool call assembles fleet arguments and decodes the result."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from graph_os.fleet_tool_call import (
    FleetToolCallError,
    call_fleet_tool,
    run_coroutine,
)


class _Client:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def call_tool(self, tool: str, arguments: dict) -> object:
        if self.fail:
            raise RuntimeError("down")
        self.calls.append((tool, arguments))
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"ok": 1}))])


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        ("json", {"action": "list", "params_json": '{"a": 1}'}),
        ("args", {"action": "list", "a": 1}),
    ],
)
def test_arguments_and_decode(style: str, expected: dict) -> None:
    client = _Client()
    result = run_coroutine(
        call_fleet_tool(
            tool="t", action="list", params={"a": 1}, params_style=style, client=client
        )
    )
    assert result == {"ok": 1}
    assert client.calls == [("t", expected)]


def test_failure_is_typed_and_hides_arguments() -> None:
    with pytest.raises(FleetToolCallError) as info:
        run_coroutine(
            call_fleet_tool(tool="t", params={"secret": "v"}, client=_Client(True))
        )
    assert "v" not in str(info.value).replace("fleet", "")


def test_unconfigured_server_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text('{"mcpServers": {}}')
    monkeypatch.setenv("MCP_CONFIG", str(config))
    with pytest.raises(FleetToolCallError):
        run_coroutine(call_fleet_tool(tool="t", server="missing"))
