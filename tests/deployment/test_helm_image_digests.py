"""The Helm chart renders digest-pinned images only when values pin them."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from arbora.api.domain.deployment.artifacts import select_artifact, verify_digests

PINNED_VALUES = Path(__file__).parent / "fixtures" / "helm-pinned-values.yaml"
HELM = shutil.which("helm")

pytestmark = pytest.mark.skipif(HELM is None, reason="helm is not installed")


def _render(tmp_path: Path, *value_files: Path) -> Path:
    assert HELM is not None
    tmp_path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, KUBECONFIG=os.devnull)
    for name in ("HELM_CACHE_HOME", "HELM_CONFIG_HOME", "HELM_DATA_HOME"):
        directory = tmp_path / name.lower()
        directory.mkdir()
        env[name] = str(directory)
    command = [
        HELM,
        "template",
        "fixture",
        str(select_artifact("helm")),
        "--kube-version",
        "1.31.0",
    ]
    for path in value_files:
        command += ["--values", str(path)]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=60, check=False, env=env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rendered = tmp_path / "rendered.yaml"
    rendered.write_text(result.stdout, encoding="utf-8")
    return rendered


# spec: ARBORA-DEPLOY-R001
def test_default_render_fails_and_pinned_render_passes(tmp_path: Path) -> None:
    floating = verify_digests([_render(tmp_path / "a")])
    assert not floating.ok
    assert "IMAGE_NOT_DIGEST_PINNED" in floating.errors[0]
    pinned = verify_digests([_render(tmp_path / "b", PINNED_VALUES)])
    assert pinned.ok, pinned.errors


# spec: ARBORA-DEPLOY-R017.14
def test_pinned_render_passes_and_default_render_fails_on_replace_me(
    tmp_path: Path,
) -> None:
    pinned = verify_digests([_render(tmp_path / "a", PINNED_VALUES)])
    assert pinned.ok, pinned.errors
    assert pinned.checked == 3  # arbora, engine and the connector extension
    default = verify_digests([_render(tmp_path / "b")])
    assert not default.ok
    assert any("IMAGE_NOT_DIGEST_PINNED" in error for error in default.errors)
