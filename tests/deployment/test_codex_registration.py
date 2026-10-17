"""Current-only Codex registration tests.

Codex MCP servers are managed through ``codex mcp``/``config.toml``.  These
tests intentionally reject the retired client-side JSON shape and any launcher
state that could persist credentials or bind a deployment to one machine.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from typing import Any

import pytest

from arbora.deployment.codex_registration import (
    CodexRegistrationError,
    arbora_stdio_spec,
    register_codex_arbora,
)


def _result(
    returncode: int = 0,
    *,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _runner(
    results: Iterable[subprocess.CompletedProcess[str]],
) -> tuple[list[list[str]], Any]:
    pending = iter(results)
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "timeout": 20,
            "check": False,
        }
        return next(pending)

    return calls, run


def _canonical_payload() -> str:
    return json.dumps(
        {
            "name": "arbora",
            "enabled": True,
            "disabled_reason": None,
            "startup_timeout_sec": None,
            "tool_timeout_sec": None,
            "enabled_tools": None,
            "disabled_tools": None,
            "transport": {
                "type": "stdio",
                "command": "arbora",
                "args": ["--transport", "stdio"],
                "cwd": None,
                "env": None,
                "env_vars": [],
            },
        }
    )


def test_portable_spec_has_no_environment_secret_or_machine_path() -> None:
    spec = arbora_stdio_spec()
    assert spec == {"command": "arbora", "args": ["--transport", "stdio"]}
    assert not {"env", "cwd", "url", "headers"}.intersection(spec)


def test_missing_registration_uses_exact_codex_add_command() -> None:
    calls, run = _runner([_result(1), _result()])

    result = register_codex_arbora(runner=run)

    assert calls == [
        ["codex", "mcp", "get", "arbora", "--json"],
        [
            "codex",
            "mcp",
            "add",
            "arbora",
            "--",
            "arbora",
            "--transport",
            "stdio",
        ],
    ]
    assert result == {
        "status": "registered",
        "server": "arbora",
        "transport": "stdio",
    }


def test_canonical_registration_is_idempotent() -> None:
    calls, run = _runner([_result(stdout=_canonical_payload())])

    result = register_codex_arbora(runner=run)

    assert calls == [["codex", "mcp", "get", "arbora", "--json"]]
    assert result["status"] == "unchanged"


@pytest.mark.parametrize(
    "stale_transport",
    [
        {
            "type": "stdio",
            "command": "/machine/specific/bin/arbora",
            "args": [],
            "cwd": "/machine/specific/workspace",
            "env": {"TOKEN": "do-not-retain"},
            "env_vars": [],
        },
        {
            "type": "stdio",
            "command": "arbora",
            "args": ["--transport", "stdio"],
            "cwd": None,
            "env": None,
            "env_vars": ["TOKEN"],
        },
    ],
)
def test_stale_registration_is_replaced_without_copying_state(
    stale_transport: dict[str, Any],
) -> None:
    stale = json.dumps(
        {"name": "arbora", "enabled": True, "transport": stale_transport}
    )
    calls, run = _runner([_result(stdout=stale), _result(), _result()])

    result = register_codex_arbora(runner=run)

    assert calls[1:] == [
        ["codex", "mcp", "remove", "arbora"],
        [
            "codex",
            "mcp",
            "add",
            "arbora",
            "--",
            "arbora",
            "--transport",
            "stdio",
        ],
    ]
    retained = json.dumps({"calls": calls, "result": result})
    assert "do-not-retain" not in retained
    assert "/machine/specific" not in retained
    assert result["status"] == "replaced"


def test_command_failure_does_not_expose_captured_output() -> None:
    calls, run = _runner(
        [
            _result(1, stderr="secret-value /machine/specific/path"),
            _result(1, stderr="different-secret"),
        ]
    )

    with pytest.raises(CodexRegistrationError) as error:
        register_codex_arbora(runner=run)

    assert calls[-1][1:4] == ["mcp", "add", "arbora"]
    message = str(error.value)
    assert "secret-value" not in message
    assert "different-secret" not in message
    assert "/machine/specific" not in message


def test_cli_registers_codex_and_retired_json_subcommand_is_absent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from arbora.deployment import cli

    monkeypatch.setattr(
        cli,
        "register_codex_arbora",
        lambda: {
            "status": "unchanged",
            "server": "arbora",
            "transport": "stdio",
        },
    )
    assert cli.main(["codex"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "unchanged"

    with pytest.raises(SystemExit) as error:
        cli.main(["mcp"])
    assert error.value.code == 2
