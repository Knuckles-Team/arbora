"""``arbora plugin bundle`` (ARBORA-HARNESS-PLUGINS-R001.4, R001.5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from arbora.harness_plugins import cli
from tests.harness_plugins.conftest import ENDPOINT


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R001.4")
def test_bundle_print_path_prints_one_absolute_line(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        cli.main(
            [
                "bundle",
                "--harness",
                "claude-code",
                "--endpoint",
                ENDPOINT,
                "--print-path",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    path = Path(out.strip())
    assert path.is_absolute() and (path / ".claude-plugin/plugin.json").is_file()
    assert path.parent.parent == home / ".cache/arbora/plugins"
    assert path.parent.name == "claude-code"
    out_dir = home / "explicit"
    assert (
        cli.main(
            [
                "bundle",
                "--harness",
                "opencode",
                "--endpoint",
                ENDPOINT,
                "--out",
                str(out_dir),
                "--print-path",
            ]
        )
        == 0
    )
    assert Path(capsys.readouterr().out.strip()) == out_dir.resolve()


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R001.5")
def test_refusals_exit_two_and_write_nothing(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def files() -> list[Path]:
        return sorted(p for p in home.rglob("*") if p.is_file())

    before = files()
    cases = [
        (["bundle", "--harness", "devin", "--endpoint", ENDPOINT], "HARNESS_UNKNOWN"),
        (["bundle", "--harness", "claude-code"], "ENDPOINT_UNSET"),
        (
            [
                "bundle",
                "--harness",
                "claude-code",
                "--endpoint",
                ENDPOINT,
                "--skill",
                "nope",
            ],
            "SKILL_UNKNOWN",
        ),
    ]
    for argv, code in cases:
        assert cli.main(argv) == 2
        assert code in capsys.readouterr().err
        assert files() == before
