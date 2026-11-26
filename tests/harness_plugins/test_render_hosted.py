"""The hosted profile renders a connection sheet and a playbook (R010.1, R010.3)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from arbora.harness_plugins import cli
from arbora.harness_plugins.compose import compose_bundle
from arbora.harness_plugins.profiles import profile
from arbora.harness_plugins.render import TreeRefused, render
from tests.harness_plugins.conftest import ENDPOINT

CONTRACT = {
    "admin_url": "https://idp.example.test",
    "realm": "r",
    "admin_client_id": "a",
    "admin_client_secret_ref": "ref",
    "hosted_redirect_uris": ["https://vendor.example.test/oauth/callback"],
}


def _spec(skill_root: Path, endpoint: str = ENDPOINT):  # type: ignore[no-untyped-def]
    return compose_bundle(
        profile("hosted"), base_url=endpoint, skill_roots=[skill_root]
    )


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R010.1")
def test_connection_sheet_and_playbook_only(skill_root: Path, tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    render(_spec(skill_root), tree)

    assert sorted(p.name for p in tree.rglob("*") if p.is_file()) == [
        "connection.md",
        "playbook.md",
    ]
    sheet = (tree / "connection.md").read_text()
    assert f"{ENDPOINT}/mcp" in sheet
    assert "streamable HTTP" in sheet
    assert "arbora-hosted-mcp" in sheet
    playbook = (tree / "playbook.md").read_text()
    assert "## Skill: canopy" in playbook and "## Skill: session-handoff" in playbook

    for endpoint in (
        "http://door.example.test",
        "https://localhost:8443",
        "https://127.0.0.1",
    ):
        with pytest.raises(TreeRefused) as refused:
            render(_spec(skill_root, endpoint), tmp_path / "refused")
        assert refused.value.code == "HOSTED_ENDPOINT_UNREACHABLE"


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R010.1")
def test_install_refuses_a_hosted_harness_and_prints_the_steps(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = argparse.Namespace(
        harness="hosted", endpoint=ENDPOINT, out=None, skills_root=None, skill=None
    )
    with pytest.raises(cli.Refused) as refused:
        cli.install_command(args)
    assert refused.value.code is cli.BundleRefusal.HOSTED_HARNESS
    err = capsys.readouterr().err
    assert "connection.md" in err and "playbook.md" in err


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R010.2")
def test_hosted_bundle_refuses_without_the_configured_client(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = argparse.Namespace(
        out=str(tmp_path / "out"),
        endpoint=ENDPOINT,
        skills_root=None,
        skill=None,
        print_path=True,
    )
    monkeypatch.delenv("ARBORA_IDENTITY_CLIENT_CONTRACT", raising=False)
    with pytest.raises(cli.Refused) as unset:
        cli.build("hosted", args, cli.inst.load_config())
    assert unset.value.code is cli.BundleRefusal.HOSTED_CLIENT_UNSET

    monkeypatch.setenv("ARBORA_IDENTITY_CLIENT_CONTRACT", json.dumps(CONTRACT))
    digest, path = cli.build("hosted", args, cli.inst.load_config())
    assert len(digest) == 64 and (path / "connection.md").is_file()


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R010.3")
def test_playbook_attaches_and_checkpoints(skill_root: Path, tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    render(_spec(skill_root), tree)
    playbook = (tree / "playbook.md").read_text()

    attach = playbook.index("workstream.attach")
    checkpoint = playbook.index("workstream.checkpoint")
    assert attach < checkpoint
    assert "<remote>#<branch>" in playbook
    assert 'harness="hosted"' in playbook
