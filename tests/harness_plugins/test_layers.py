"""Six configuration layers that only add (ARBORA-HARNESS-PLUGINS-R013)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arbora.harness_plugins import cli, door
from arbora.harness_plugins.compose import compose_bundle
from arbora.harness_plugins.layers import (
    ORDER,
    Layer,
    LayerRefused,
    file_layers,
    resolve,
    served_layers,
)
from arbora.harness_plugins.profiles import profile
from tests.harness_plugins.conftest import ENDPOINT, write_skill


def _base(skill_root: Path) -> Any:
    return compose_bundle(
        profile("claude-code"), base_url=ENDPOINT, skill_roots=[skill_root]
    )


def _toml(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R013")
def test_six_layers_add_only_and_removal_or_loosening_refuses(
    home: Path,
    skill_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("one", "two", "three"):
        write_skill(skill_root, name)
    base = _base(skill_root)
    # Each layer adds; the policy only tightens and the order is fixed.
    layers = [
        Layer("local", {"tools": ["t-local"], "policy": {"max_tools": 4}}),
        Layer(
            "company",
            {
                "skills": ["one"],
                "personas": ["support"],
                "policy": {"confirm_writes": True, "max_tools": 10},
            },
        ),
        Layer("team", {"skills": ["two"], "policy": {"max_tools": 8}}),
        Layer("user", {"skills": ["three"], "hooks": ["skill_end"]}),
    ]
    spec = resolve(layers, base, [skill_root])
    assert {s.name for s in spec.skills} >= {"one", "two", "three"}
    assert spec.personas == ("support",) and spec.tools == ("t-local",)
    assert dict(spec.policy) == {"confirm_writes": True, "max_tools": 4}
    assert ORDER == ("company", "team", "repository", "directory", "user", "local")
    # Removal in any form refuses; nothing is dropped from an earlier layer.
    for removing in (
        {"skills": ["-one"]},
        {"remove": {"skills": ["one"]}},
        {"remove_tools": ["x"]},
    ):
        with pytest.raises(LayerRefused) as refused:
            resolve(
                [Layer("company", {"skills": ["one"]}), Layer("user", removing)],
                base,
                [skill_root],
            )
        assert refused.value.code == "LAYER_REMOVES_ENTRY"
    for earlier, later in (
        ({"max_tools": 4}, {"max_tools": 9}),
        ({"confirm_writes": True}, {"confirm_writes": False}),
    ):
        with pytest.raises(LayerRefused) as loosened:
            resolve(
                [
                    Layer("company", {"policy": earlier}),
                    Layer("local", {"policy": later}),
                ],
                base,
                [skill_root],
            )
        assert loosened.value.code == "LAYER_LOOSENS_POLICY"
    # The served read yields company and team; the files give the other four.
    served = served_layers(
        {
            "layers": [
                {
                    "layer": "company",
                    "scope": "c",
                    "revision": 1,
                    "settings": {"skills": ["one"]},
                },
                {"layer": "team", "scope": "t", "revision": 1, "settings": {}},
                {"layer": "other", "scope": "x", "settings": {"skills": ["no"]}},
            ]
        }
    )
    assert [layer.name for layer in served] == ["company", "team"]
    (home / ".git").mkdir()
    _toml(home / ".arbora/bundle.toml", 'skills = ["extra-skill"]\n')
    _toml(home / ".arbora/bundle.local.toml", "[policy]\nmax_tools = 3\n")
    _toml(home / "user/bundle.toml", 'tools = ["t-user"]\n')
    names = [layer.name for layer in file_layers(home, home / "user")]
    assert names == ["repository", "directory", "user", "local"]

    # Through the command: a removing layer exits 2 and writes no file.
    _toml(home / ".arbora/bundle.local.toml", 'skills = ["-extra-skill"]\n')
    out = home / "tree"
    code = cli.main(
        [
            "bundle",
            "--harness",
            "claude-code",
            "--endpoint",
            ENDPOINT,
            "--out",
            str(out),
        ]
    )
    assert code == 2 and not out.exists()
    assert "LAYER_REMOVES_ENTRY" in capsys.readouterr().err
    _toml(home / ".arbora/bundle.local.toml", "[policy]\nmax_tools = 3\n")
    assert (
        cli.main(
            [
                "bundle",
                "--harness",
                "claude-code",
                "--endpoint",
                ENDPOINT,
                "--out",
                str(out),
            ]
        )
        == 0
    )
    config = json.loads((out / "bundle-config.json").read_text())
    assert config["policy"] == {"max_tools": 3}
    assert (out / "skills/extra-skill/SKILL.md").is_file()
    # The served company and team layers come from config.layers.get.
    monkeypatch.setattr(cli, "_door_token", lambda prof: "token")
    asked: list[str] = []

    def call(base_url: str, op: str, params: Any, token: str, **_: Any) -> Any:
        asked.append(op)
        return {
            "layers": [
                {
                    "layer": "company",
                    "scope": "c",
                    "settings": {"policy": {"max_tools": 2}},
                }
            ]
        }

    monkeypatch.setattr(door, "call", call)
    # The company ceiling of 2 is looser-checked against the local 3: refused.
    code = cli.main(
        [
            "bundle",
            "--harness",
            "claude-code",
            "--endpoint",
            ENDPOINT,
            "--out",
            str(out),
        ]
    )
    assert asked == ["config.layers.get"] and code == 2
    assert "LAYER_LOOSENS_POLICY" in capsys.readouterr().err
