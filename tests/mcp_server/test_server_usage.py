"""``arbora --help`` and ``--version`` answer without loading configuration."""

from __future__ import annotations

import json

import pytest

from arbora._version import __version__
from arbora.mcp_server import server


@pytest.fixture
def unloadable_config(tmp_path, monkeypatch):
    """A config.json the runtime loader refuses, and a failing secret reference."""
    config_dir = tmp_path / "arbora"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({"MCP_ALWAYS_LOAD": ["x"]}))
    monkeypatch.setenv("ARBORA_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OIDC_CLIENT_SECRET_REF", "openbao://absent/secret")

    def refuse(**_kwargs):
        raise AssertionError("configuration was loaded")

    monkeypatch.setattr("arbora.core.settings.load_config", refuse)


def test_version_needs_no_configuration(unloadable_config, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["arbora", "--version"])

    server.mcp_server()

    assert capsys.readouterr().out.strip() == f"arbora {__version__}"


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_needs_no_configuration(unloadable_config, monkeypatch, capsys, flag):
    monkeypatch.setattr("sys.argv", ["arbora", flag])

    server.mcp_server()

    out = capsys.readouterr().out
    assert out.startswith("usage: arbora")
    assert "--transport" in out
    assert "--version" in out
