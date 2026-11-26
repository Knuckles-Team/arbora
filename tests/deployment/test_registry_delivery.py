"""Every deploy delivers the current registry manifest and proves it readable (R021)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from arbora.deployment.production_ops import (
    ProductionOperationError,
    deliver_registry_manifest,
)

OLD = "subdirectories:\n  agents:\n    repositories: []\n"
NEW = (
    "subdirectories:\n  agents:\n    subdirectories:\n      connectors:\n"
    "        repositories: []\n"
)


@pytest.mark.spec("ARBORA-DEPLOY-R021")
def test_deploy_delivers_workspace_manifest_and_fails_on_unreadable_registry(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "workspace.yml").write_text(OLD)  # an older delivered copy
    source = tmp_path / "canonical.yml"
    source.write_text(NEW)

    report = deliver_registry_manifest(source, config)

    # The newer manifest replaces the older one, byte for byte.
    assert (config / "workspace.yml").read_text() == NEW
    assert report["sha256"] == hashlib.sha256(NEW.encode()).hexdigest()
    assert report["ok"] is True

    # A missing source fails naming the cause.
    with pytest.raises(ProductionOperationError, match="REGISTRY_SOURCE_MISSING"):
        deliver_registry_manifest(tmp_path / "absent.yml", config)

    # A delivered manifest without the connectors group fails verify, named.
    source.write_text(OLD)
    with pytest.raises(ProductionOperationError, match="registry_unreadable"):
        deliver_registry_manifest(source, config)
