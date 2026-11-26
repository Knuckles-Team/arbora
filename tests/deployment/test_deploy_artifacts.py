"""Artifact selection, the shipped files' digest posture and the environment policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arbora.deployment.artifacts import (
    ArtifactRefused,
    DigestCheckDecision,
    default_deploy_root,
    digest_check_decision,
    select_artifact,
    verify_digests,
)

DIGEST = "0123456789abcdef" * 4
PINNED = f"registry.example/arbora@sha256:{DIGEST}"
ROOT = default_deploy_root()


# spec: ARBORA-DEPLOY-R017.6
def test_each_target_resolves_to_an_existing_file_and_systemd_is_refused() -> None:
    assert select_artifact("compose") == ROOT / "compose" / "compose.yaml"
    assert select_artifact("helm") == ROOT / "helm" / "arbora"
    assert select_artifact("swarm") == ROOT / "swarm" / "stack.yml"
    assert all(
        select_artifact(target).exists() for target in ("compose", "helm", "swarm")
    )
    with pytest.raises(ArtifactRefused) as refused:
        select_artifact("systemd")
    assert refused.value.code == "unsupported_target"


# spec: ARBORA-DEPLOY-R017.6
def test_a_missing_shipped_file_refuses_rather_than_guessing(tmp_path: Path) -> None:
    with pytest.raises(ArtifactRefused) as refused:
        select_artifact("compose", deploy_root=tmp_path)
    assert refused.value.code == "ARTIFACT_MISSING"


# spec: ARBORA-DEPLOY-R017.15
def test_systemd_refused_and_others_resolve() -> None:
    with pytest.raises(ArtifactRefused, match="unsupported_target") as refused:
        select_artifact("systemd")
    assert "docs/deployment.md" in refused.value.detail
    for target in ("compose", "helm", "swarm"):
        assert select_artifact(target).exists()
    with pytest.raises(ArtifactRefused, match="unsupported_target"):
        select_artifact("nomad")


# spec: ARBORA-DEPLOY-R017.13
def test_shipped_compose_and_swarm_files_pass_template_and_resolve_modes() -> None:
    files = [select_artifact("compose"), select_artifact("swarm")]
    for path in files:
        template = verify_digests([path])
        assert template.ok, template.errors
        assert template.checked == 1
        resolved = verify_digests([path], values={"ARBORA_IMAGE": PINNED})
        assert resolved.ok, resolved.errors
        assert resolved.checked == 1
        tagged = verify_digests(
            [path], values={"ARBORA_IMAGE": "registry.example/arbora:latest"}
        )
        assert not tagged.ok
        assert "IMAGE_NOT_DIGEST_PINNED" in tagged.errors[0]


# spec: ARBORA-DEPLOY-R017.16
def test_prod_and_test_require_the_check_dev_reports_skipped_by_policy() -> None:
    assert digest_check_decision("prod") == DigestCheckDecision(required=True)
    assert digest_check_decision("test") == DigestCheckDecision(required=True)
    dev = digest_check_decision("dev")
    assert dev.required is False
    assert dev.note == "skipped by policy"


# spec: ARBORA-DEPLOY-R017.16
def test_an_unknown_tag_policy_refuses() -> None:
    class Release:
        tag_policy = "latest-wins"

    class Profile:
        release = Release()

    def loader(_name: str) -> Any:
        return Profile()

    with pytest.raises(ArtifactRefused) as refused:
        digest_check_decision("prod", loader=loader)
    assert refused.value.code == "TAG_POLICY_UNKNOWN"
