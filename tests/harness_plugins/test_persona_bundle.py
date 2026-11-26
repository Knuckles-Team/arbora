"""A role persona selects the installed skill set (ARBORA-HARNESS-PLUGINS-R014)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arbora.harness_plugins import cli, door
from tests.harness_plugins.conftest import ENDPOINT


def _answer(skills: list[str]) -> dict[str, Any]:
    return {
        "records": [
            {
                "answer": {
                    "type": "load_set",
                    "value": {
                        "skills": skills,
                        "tiered_skills": [
                            {"skill_ref": s, "tier": "core"} for s in skills
                        ],
                    },
                }
            }
        ]
    }


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R014")
def test_persona_set_and_always_on_rendered_and_unknown_refused(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_door_token", lambda prof: "token")
    asked: list[tuple[str, Any]] = []

    def call(base_url: str, op: str, params: Any, token: str, **_: Any) -> Any:
        asked.append((op, dict(params)))
        if op == "config.layers.get":
            return {"layers": []}
        if params["persona"] == "nobody":
            raise door.DoorRefused("INVALID_ARGUMENT", "PERSONA_UNKNOWN")
        return _answer(["extra-skill"])

    monkeypatch.setattr(door, "call", call)
    out = home / "tree"
    argv = [
        "bundle",
        "--harness",
        "claude-code",
        "--endpoint",
        ENDPOINT,
        "--out",
        str(out),
    ]
    # The declared skill list is not used: the persona set plus the door skills.
    code = cli.main([*argv, "--persona", "support", "--skill", "session-handoff"])
    assert code == 0
    assert ("skills.persona.get", {"persona": "support"}) in asked
    skills = sorted(p.parent.name for p in (out / "skills").glob("*/SKILL.md"))
    assert skills == ["canopy", "extra-skill", "session-handoff"]
    manifest = json.loads((out / ".claude-plugin/plugin.json").read_text())
    assert manifest["persona"] == "support"
    assert len(manifest["loadSetDigest"]) == 64
    # A persona the graph lacks refuses and writes no file.
    gone = home / "gone"
    code = cli.main(
        [
            "bundle",
            "--harness",
            "claude-code",
            "--endpoint",
            ENDPOINT,
            "--out",
            str(gone),
            "--persona",
            "nobody",
        ]
    )
    assert code == 2 and not gone.exists()
    assert "PERSONA_UNKNOWN" in capsys.readouterr().err
