"""Secrets default to the vault and the trust bundle reaches every component (ARBORA-DEPLOY-R013.7)."""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import pytest

from arbora.deployment import trust_bundle
from tests.api._loopback_engine import loopback  # noqa: F401
from tests.api.test_deploy_ops_served import _apply_params, _assembling

# A public self-signed certificate used only as a bundle file; it holds no key.
BUNDLE = """-----BEGIN CERTIFICATE-----
MIIDFTCCAf2gAwIBAgIUYfboc5sqpfGab0KXpdFBtFvxCiowDQYJKoZIhvcNAQEL
BQAwGTEXMBUGA1UEAwwOYXJib3JhLXRlc3QtY2EwIBcNMjYxMTI2MjE0ODUwWhgP
MjEyNjExMDIyMTQ4NTBaMBkxFzAVBgNVBAMMDmFyYm9yYS10ZXN0LWNhMIIBIjAN
BgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAuU3aC8srQ/75s2cHtU9pzESETcQI
Rvb3u9CIv26mba9eOV848u+NR9itLJf+c5GSpjuO9Nd5j1UVTXgPtXoAg25lDBCv
5h0T4v4B8vktGpEGuGNjwTewaaeZqJdFZkt6CLFr2+Yk3Wlf3BNKlDNItp/uo4jL
pU3EsTzwQx8RvoII3y8/vpHVI8OmT2+KSm89jwEIxWlJnLtcDiKBCCNHR51Akbhe
44PKX3mHhRc7VwfLJQuAjs3/OWi8hut+gezhrZqJM4tRywqfsvOjoLZvtvZc27bY
SdzD1/eDB+ucSiajmyJnEW4i4zqC/aRC5+AcqTSxRfgZJJyeiG21lpVeGQIDAQAB
o1MwUTAdBgNVHQ4EFgQU2sgLWGKoF9mx2I795sNoZLFKRLswHwYDVR0jBBgwFoAU
2sgLWGKoF9mx2I795sNoZLFKRLswDwYDVR0TAQH/BAUwAwEB/zANBgkqhkiG9w0B
AQsFAAOCAQEAQkQqk6ruLUZ6OMvQQZ4BXTx54mnzu0lGQKyjEsb+NZVE1YU/7zgh
jytQh2Jdn2d+9HfzoQiMS9Vpsa9R7z5zCo8M5AcezY6gfRvAQIBJJcfy64JPetch
3MWqO60o8WW4BXhGThxxBtZ3jHKVsRyL8CP/3/w+ChyIyVEZt7xrDyRnVEEqXoUN
8sFzsisaaJcYHoHoWnsmOwfIoI9APkLCiQxf52lhpnQPcB41rLvz4Bf7nWYeoiHd
7uTsVGODZ1R8p1yDjQxbzgSX6/zII332KhalmZWboh2VMFgRnNXiHMxvAX1jJbv7
uplghKKZ8Z/PUOt0UZwkq9MllVRfxeGIxg==
-----END CERTIFICATE-----
"""
COMPONENTS = ["tool:a", "tool:b"]


def _bundle(tmp_path: Path) -> str:
    path = tmp_path / "enterprise-ca.pem"
    path.write_text(BUNDLE)
    return str(path)


def _connection_log(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, str]]:
    seen: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        trust_bundle,
        "tls_connect",
        lambda host, port, bundle: seen.append((host, port, bundle)),
    )
    return seen


@pytest.mark.spec("ARBORA-DEPLOY-R013.7")
def test_every_touched_component_receives_and_verifies_the_bundle(
    loopback: Any,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _connection_log(monkeypatch)
    bundle = _bundle(tmp_path)
    endpoints = {
        name: f"{name}.internal:8443"
        for name in (*trust_bundle.CORE_COMPONENTS, *COMPONENTS)
    }
    loopback.config.update(
        {
            "tools": {"trust_mount": {"server": "ops", "tool": "mount_trust"}},
            "trust": {"bundle_path": bundle, "endpoints": endpoints},
        }
    )
    _assembling(loopback)
    params = _apply_params(loopback, COMPONENTS, [])
    # The confirmed digest binds the configuration that carries the trust rows.
    from arbora.deployment.assembly import config_digest

    params["config_digest"] = config_digest(loopback.config)

    done = loopback.confirmed("deploy.placement.apply", params, "trust-1")

    value = loopback.value(done)
    mounted = {row["component"] for row in value["trust"]}
    assert mounted == {*trust_bundle.CORE_COMPONENTS, *COMPONENTS}
    assert all(
        row["verify"] and row["env_var"] == "SSL_CERT_FILE" for row in value["trust"]
    )
    assert {host for host, _, _ in seen} == {f"{n}.internal" for n in mounted}
    assert {path for _, _, path in seen} == {bundle}
    assert loopback.steps.tools.count("mount_trust") == len(mounted)


@pytest.mark.spec("ARBORA-DEPLOY-R013.7")
def test_the_tls_check_never_disables_certificate_verification(tmp_path: Path) -> None:
    context = ssl.create_default_context(cafile=_bundle(tmp_path))
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    source = Path(trust_bundle.__file__).read_text()
    assert "CERT_NONE" not in source and "_create_unverified_context" not in source


@pytest.mark.spec("ARBORA-DEPLOY-R013.7")
def test_a_missing_bundle_and_a_failed_check_refuse_naming_the_component(
    loopback: Any,  # noqa: F811
    tmp_path: Path,
) -> None:
    loopback.config.update(
        {
            "tools": {"trust_mount": {"server": "ops", "tool": "mount_trust"}},
            "trust": {"bundle_path": str(tmp_path / "absent.pem")},
        }
    )
    _assembling(loopback)
    params = _apply_params(loopback, COMPONENTS, [])
    from arbora.deployment.assembly import config_digest

    params["config_digest"] = config_digest(loopback.config)
    refused = loopback.confirmed("deploy.placement.apply", params, "trust-2")
    assert refused.json()["error"]["code"] == "TRUST_BUNDLE_MISSING", refused.text

    loopback.config["trust"] = {"bundle_path": _bundle(tmp_path), "endpoints": {}}
    params["config_digest"] = config_digest(loopback.config)
    refused = loopback.confirmed("deploy.placement.apply", params, "trust-3")
    assert refused.json()["error"]["code"] == "TRUST_VERIFY_FAILED", refused.text


@pytest.mark.spec("ARBORA-DEPLOY-R013.7")
def test_inline_secret_is_refused_and_vault_reference_is_written(
    loopback: Any,  # noqa: F811
) -> None:
    from arbora.deployment.assembly import config_digest

    _assembling(loopback)
    loopback.config.update({"engine": {"api_token": "plain-value"}})
    params = _apply_params(loopback, COMPONENTS, [])
    params["config_digest"] = config_digest(loopback.config)

    refused = loopback.confirmed("deploy.placement.apply", params, "secret-1")
    assert refused.json()["error"]["code"] == "SECRET_INLINE_REFUSED", refused.text
    assert "plain-value" not in refused.text
    assert loopback.steps.calls == []

    loopback.config["tools"] = {"secret_write": {"server": "vault", "tool": "kv_put"}}
    params["config_digest"] = config_digest(loopback.config)
    done = loopback.confirmed("deploy.placement.apply", params, "secret-2")

    value = loopback.value(done)
    assert value["stored_secrets"] == ["engine.api_token"]
    written = [args for tool, args, _ in loopback.steps.calls if tool == "kv_put"]
    assert written == [{"path": "openbao://engine/api_token", "value": "plain-value"}]
    assert "plain-value" not in str(value)
