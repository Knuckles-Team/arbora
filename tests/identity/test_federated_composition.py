"""The federated routes are composed into the served browser boundary (R009.2.3, R012.2.3.2)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.identity import federated
from arbora.identity.engine import IdentityUnavailable
from arbora.security.key_cache import JwksCache
from tests.identity._federated_fixture import OIDC_CONFIG, SAML_CONFIG


@pytest.mark.pin_lag
@pytest.mark.spec("ARBORA-IDENTITY-R009.2.3")
def test_providers_in_settings_build_a_federation_and_none_builds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "ARBORA_IDENTITY_OIDC_PROVIDERS": OIDC_CONFIG,
        "ARBORA_IDENTITY_SAML_PROVIDERS": SAML_CONFIG,
    }
    monkeypatch.setattr(
        "arbora.core.environment.read_env",
        lambda name, fallback=None: values.get(name, fallback),
    )
    built = federated.federation_from_settings(object(), "g", "https://console.invalid")
    assert (
        built is not None
        and set(built._oidc) == {"corp"}
        and set(built._saml) == {"saml1"}
    )
    values.clear()
    assert (
        federated.federation_from_settings(object(), "g", "https://x.invalid") is None
    )
    values["ARBORA_IDENTITY_OIDC_PROVIDERS"] = json.dumps([{"id": "a.b"}])
    with pytest.raises(IdentityUnavailable):
        federated.federation_from_settings(object(), "g", "https://x.invalid")


@pytest.mark.pin_lag
@pytest.mark.spec("ARBORA-IDENTITY-R012.2.3.2")
def test_browser_boundary_hands_the_federation_to_the_auth_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.identity import (
        admission,
        broker,
        browser_composition,
        browser_runtime,
        composition,
        issuer,
        issuer_discovery,
    )
    from arbora.mcp_server import browser_app

    sentinel = object()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(federated, "federation_from_settings", lambda *a, **k: sentinel)
    monkeypatch.setattr(
        composition,
        "build_engine_identity_ports",
        lambda *a, **k: SimpleNamespace(sessions=object(), credentials=object()),
    )
    for module, name in (
        (issuer, "LocalIssuer"),
        (issuer_discovery, "IssuerDiscovery"),
        (broker, "ArboraBrowserAuthority"),
        (admission, "BrowserAdmission"),
    ):
        monkeypatch.setattr(module, name, lambda *a, **k: object())
    monkeypatch.setattr(issuer, "IssuerSettings", lambda *a, **k: object())
    monkeypatch.setattr(
        browser_app, "create_browser_api_application", lambda **k: object()
    )
    monkeypatch.setattr(
        browser_app,
        "create_webui_session_boundary",
        lambda **k: captured.update(k) or object(),
    )
    monkeypatch.setattr(
        browser_runtime,
        "BrowserAuthApplication",
        lambda *a, **k: SimpleNamespace(federation=k["federation"]),
    )
    browser_composition.build_browser_boundary(
        SimpleNamespace(
            services=SimpleNamespace(policy_gate=SimpleNamespace(check_op=None))
        ),
        object(),
        graph="g",
        tenant="t",
        origin="https://console.invalid",
        issuer="https://console.invalid",
        audience="a",
        store=object(),
        request_dispatch=None,
        keys=JwksCache(),
    )
    assert captured["auth_app"].federation is sentinel
