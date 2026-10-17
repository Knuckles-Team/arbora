"""Deployment self-checks: the production profile, the mount guard and asset readers."""

from __future__ import annotations

import pytest

from graph_os.deployment import certification_assets as assets
from graph_os.deployment import live_mount_guard as lmg
from graph_os.deployment import profile_guard as pg


@pytest.mark.parametrize(
    ("value", "production"),
    [
        ("prod", True),
        (" Production ", True),
        ("dev", False),
        ("", False),
        (None, False),
    ],
)
def test_production_profile(monkeypatch, value, production):
    monkeypatch.delenv(pg.PROFILE_ENV_VAR, raising=False)
    assert pg.is_production_profile(value) is production


@pytest.mark.parametrize(
    ("env", "reason"),
    [({"KUBERNETES_SERVICE_HOST": ""}, "not running under Kubernetes")],
)
def test_mount_guard_is_not_applicable_outside_a_pod(monkeypatch, env, reason):
    # CI runs the suite with the skip variable set; this case is about the
    # pod check itself, so the skip must not answer first.
    monkeypatch.delenv(lmg._SKIP_ENV_VAR, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    status, detail = lmg.check_live_mount_status()
    assert status is lmg.LiveMountStatus.NOT_APPLICABLE
    assert detail["reason"] == reason


def test_regular_reader_is_bounded(tmp_path):
    path = tmp_path / "asset.json"
    path.write_text('{"a": 1}')
    assert assets._read_regular(path, limit=64, code="bad") == b'{"a": 1}'
    with pytest.raises(assets.CertificationAssetError):
        assets._read_regular(path, limit=2, code="bad")


@pytest.mark.parametrize(
    ("payload", "ok"),
    [(b'{"a": 1}', True), (b'{"a": 1, "a": 2}', False), (b"not json", False)],
)
def test_json_reader_rejects_duplicate_keys(payload, ok):
    if ok:
        assert assets._json_without_duplicates(payload, code="bad") == {"a": 1}
    else:
        with pytest.raises(assets.CertificationAssetError):
            assets._json_without_duplicates(payload, code="bad")


@pytest.mark.parametrize(
    ("ttl", "ok"), [(None, True), (300, True), (10, False), (True, False)]
)
def test_token_lifetime_bounds(ttl, ok):
    if ok:
        assert assets.validated_token_ttl_seconds(ttl) >= assets.MIN_TOKEN_TTL_SECONDS
    else:
        with pytest.raises(ValueError):
            assets.validated_token_ttl_seconds(ttl)
