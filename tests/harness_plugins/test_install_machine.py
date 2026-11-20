"""The install machine, placement and the one-door cutover (ARBORA-HARNESS-PLUGINS-R002)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pyoxigraph as ox
import pytest

from arbora.harness_plugins import cli
from arbora.harness_plugins import install as inst
from arbora.harness_plugins.profiles import profile
from tests.harness_plugins.conftest import ENDPOINT, write_skill

PACK = Path(__file__).resolve().parents[2] / "arbora/ontology/state_machines.ttl"


def described_edges(machine_class: str) -> set[tuple[str, str, str]]:
    store = ox.Store()
    store.load(path=str(PACK), format=ox.RdfFormat.TURTLE)
    rows = store.query(
        f"""PREFIX : <https://knuckles-team.github.io/ontology/core#>
        PREFIX arbora: <https://knuckles-team.github.io/ontology/admission#>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT ?f ?e ?t WHERE {{
          ?x rdfs:subClassOf+ arbora:{machine_class} ; :fromState ?fs ; :firedBy ?ev ; :toState ?ts .
          ?fs :storedForm ?f . ?ev :storedForm ?e . ?ts :storedForm ?t }}"""
    )
    assert isinstance(rows, ox.QuerySolutions)
    return {tuple(str(term.value) for term in row) for row in rows}  # type: ignore[misc]


@pytest.fixture
def no_harness_cli(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(argv: Sequence[str]) -> int:
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(inst, "run_harness_cli", run)
    return calls


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R002.1")
def test_edges_match_the_ontology_and_refuse_others() -> None:
    assert described_edges("PluginInstallTransition") == {
        (s.value, e.value, t.value) for (s, e), t in inst.INSTALL_EDGES.items()
    }
    with pytest.raises(inst.IllegalInstallTransition):
        inst.transition(inst.InstallState.ABSENT, inst.InstallEvent.INSTALL)


def _snapshot(home: Path) -> set[str]:
    skip = (".cache/", ".config/arbora/plugins.json")
    return {
        p.relative_to(home).as_posix()
        for p in home.rglob("*")
        if p.is_file() and not p.relative_to(home).as_posix().startswith(skip)
    }


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R002.2")
def test_install_status_remove_touch_only_owned_files(
    home: Path, no_harness_cli: list[list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    keep = home / ".config/opencode/plugins/mine/index.ts"
    keep.parent.mkdir(parents=True)
    keep.write_text("// someone else's plugin\n")
    before = _snapshot(home)
    assert cli.main(["install", "--harness", "opencode", "--endpoint", ENDPOINT]) == 0
    placed = home / ".config/opencode/plugins/arbora"
    assert (placed / "server.ts").is_file() and (placed / "bundle.json").is_file()
    record = inst.load_config().installs["opencode"]
    assert record.state is inst.InstallState.INSTALLED and record.place == str(placed)
    assert inst.load_config().endpoint == ENDPOINT
    capsys.readouterr()
    assert cli.main(["status", "--harness", "opencode"]) == 0
    assert json.loads(capsys.readouterr().out)["opencode"]["state"] == "installed"
    assert cli.main(["remove", "--harness", "opencode"]) == 0
    assert _snapshot(home) == before and keep.is_file()
    assert inst.load_config().installs["opencode"].state is inst.InstallState.REMOVED


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R002.3")
def test_duplicate_door_refuses_and_edits_nothing(
    home: Path, no_harness_cli: list[list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    claude = home / ".claude.json"
    claude.write_text(
        json.dumps(
            {"mcpServers": {"arbora": {"type": "http", "url": ENDPOINT + "/mcp"}}}
        )
    )
    link = home / ".claude/skills/canopy"
    link.parent.mkdir(parents=True)
    link.symlink_to(home)
    jsonc = home / ".config/opencode/opencode.jsonc"
    jsonc.parent.mkdir(parents=True)
    door = f'"door": {{"type": "remote", "url": "{ENDPOINT}/mcp",}},'
    jsonc.write_text('{\n  // door\n  "mcp": {' + door + "},\n}\n")
    before = {p: p.read_bytes() for p in (claude, jsonc)}
    doors = inst.find_duplicate_doors(profile("claude-code"), home, ENDPOINT)
    assert {d.entry for d in doors} == {"mcpServers.arbora", "skill canopy"}
    assert all(d.removal for d in doors)
    assert (
        cli.main(["install", "--harness", "claude-code", "--endpoint", ENDPOINT]) == 2
    )
    err = capsys.readouterr().err
    assert "DUPLICATE_DOOR" in err and str(claude) in err and f"rm -r {link}" in err
    assert cli.main(["install", "--harness", "opencode", "--endpoint", ENDPOINT]) == 2
    assert "mcp.door" in capsys.readouterr().err
    assert {p: p.read_bytes() for p in (claude, jsonc)} == before
    assert no_harness_cli == []
    assert not (home / ".config/opencode/plugins/arbora").exists()


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R002.4")
def test_changed_registry_gives_stale(
    home: Path,
    skill_root: Path,
    no_harness_cli: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["install", "--harness", "opencode", "--endpoint", ENDPOINT]) == 0
    write_skill(skill_root, "session-handoff", "A changed handoff skill.")
    capsys.readouterr()
    assert cli.main(["status", "--harness", "opencode"]) == 0
    assert json.loads(capsys.readouterr().out)["opencode"]["state"] == "stale"
    assert cli.main(["install", "--harness", "opencode"]) == 0
    assert inst.load_config().installs["opencode"].state is inst.InstallState.INSTALLED
