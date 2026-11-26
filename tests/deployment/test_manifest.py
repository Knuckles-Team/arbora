"""The deployment manifest loader refuses malformed and floating manifests (ARBORA-DEPLOY-POLICY-R001)."""

from __future__ import annotations

from pathlib import Path

import pytest

from arbora.deployment.manifest import ManifestRefused, load_manifest

DIGEST = "registry.example/app@sha256:" + "b" * 64
GOOD = f"""schema_version: 1
name: prod
components:
  - id: gateway
    kind: gateway
    image: {DIGEST}
    revision: r1
    identity_mode: external
    secrets_ref: openbao://kv/gateway
    capacity: {{cpu_millis: 500, memory_bytes: 1024, replicas: 2}}
"""


def _load(tmp_path: Path, text: str, name: str = "prod") -> object:
    (tmp_path / "prod.yaml").write_text(text)
    return load_manifest(tmp_path, name)


# spec: ARBORA-DEPLOY-POLICY-R001
def test_unknown_key_duplicate_key_and_floating_image_are_rejected(
    tmp_path: Path,
) -> None:
    manifest = _load(tmp_path, GOOD)
    assert manifest.components[0].capacity.replicas == 2  # type: ignore[attr-defined]

    cases = {
        "unknown key": (GOOD + "extra: 1\n", "MANIFEST_INVALID"),
        "duplicate key": (
            GOOD.replace("revision: r1\n", "revision: r1\n    revision: r2\n"),
            "MANIFEST_INVALID",
        ),
        "floating tag": (
            GOOD.replace(f"@sha256:{'b' * 64}", ":latest"),
            "IMAGE_NOT_DIGEST_PINNED",
        ),
        "tag and digest": (GOOD.replace("app@", "app:1@"), "IMAGE_NOT_DIGEST_PINNED"),
        "bad secret ref": (
            GOOD.replace("openbao://kv/gateway", "plain-text"),
            "MANIFEST_INVALID",
        ),
        "bad kind": (
            GOOD.replace("kind: gateway", "kind: database"),
            "MANIFEST_INVALID",
        ),
        "bad schema": (
            GOOD.replace("schema_version: 1", "schema_version: 2"),
            "MANIFEST_INVALID",
        ),
    }
    for label, (text, code) in cases.items():
        with pytest.raises(ManifestRefused) as refused:
            _load(tmp_path, text)
        assert refused.value.code == code, label

    for bad in ("Prod", "../x", "", "a" * 65):
        with pytest.raises(ManifestRefused) as refused:
            load_manifest(tmp_path, bad)
        assert refused.value.code == "MANIFEST_INVALID"
