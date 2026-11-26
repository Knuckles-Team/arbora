"""``up`` starts the serving process and web UI co-service; ``down`` stops them (R019)."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from arbora.deployment import sandbox
from arbora.mcp_server import composition


@pytest.mark.spec("ARBORA-DEPLOY-R019")
def test_up_serves_the_real_stack_and_down_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engines: list[object] = []
    monkeypatch.setattr(
        composition,
        "ensure_local_engine",
        lambda config: (
            engines.append(config)
            or SimpleNamespace(address=f"127.0.0.1:{config.port}")
        ),
    )
    stood_down: list[object] = []
    monkeypatch.setattr(composition, "stand_down_engine", stood_down.append)
    monkeypatch.setattr(sandbox, "resolve_engine", lambda *a, **k: "pinned")
    launched: list[tuple[list[str], dict[str, str]]] = []

    def launcher(command: Sequence[str], environment: Mapping[str, str]) -> int:
        launched.append((list(command), dict(environment)))
        return 424242

    stopped: list[int] = []
    args = argparse.Namespace(
        root=str(tmp_path),
        principal="operator",
        second_tenant_principal=None,
        port=19100,
        serving_port=19101,
        webui_port=19102,
    )
    summary = sandbox.up(args, hold=False, launcher=launcher)

    assert len(launched) == 1
    command, environment = launched[0]
    assert command[-6:] == [
        "--transport",
        "streamable-http",
        "--host",
        "127.0.0.1",
        "--port",
        "19101",
    ]
    assert environment["ARBORA_AUTH_MODE"] == "none"
    assert environment["WEBUI_AUTH_MODE"] == "none"
    assert environment["ENABLE_WEB_UI"] == "true"
    assert environment["ARBORA_WEBUI_PORT"] == "19102"
    assert environment["GRAPH_SERVICE_ENDPOINTS"] == "tcp://127.0.0.1:19100"
    assert summary["serving"] == "127.0.0.1:19101"
    record = json.loads((tmp_path / sandbox.STATE_FILE).read_text())
    assert record["processes"] == [424242]

    assert sandbox.down(args, terminate=stopped.append) is True
    assert stopped == [424242]
    assert len(stood_down) == 1
    assert not (tmp_path / sandbox.STATE_FILE).exists()
    assert sandbox.down(args, terminate=stopped.append) is False
    assert stopped == [424242]
