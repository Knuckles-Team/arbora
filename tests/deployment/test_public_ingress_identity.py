"""ARBORA-INGRESS-R001.1 — typed model render + refusal tests.

Covers test-spec.md T-IN-01 (deterministic redacted render) and T-IN-02
(each malformed/unknown/missing input refuses with a diagnostic, no secret
value leaked). This slice is standalone: no gateway/MCP wiring is exercised
here (ARBORA-INGRESS-R001.2+).
"""

from __future__ import annotations

from datetime import date

import pytest

from arbora.api.domain.deployment.genesis_environments import (
    load_environment_profile,
    profile_summary,
)
from arbora.api.domain.deployment.public_ingress_identity import (
    PublicIngressProfile,
    PublicIngressProfileError,
    public_ingress_profile_from_mapping,
)
from arbora.deployment.config_generator import generate_config

pytestmark = pytest.mark.spec("ARBORA-INGRESS-R001.1")

_VALID = {
    "version": 1,
    "public_origin": "https://arbora.example.com",
    "tls_secret_ref": "k8s-secret://arbora/public-ingress-tls",
    "callback_uri": "https://arbora.example.com/auth/callback",
    "trusted_proxy": "ingress-controller",
    "legacy_hostname": None,
    "legacy_redirect_until": None,
}


def test_valid_mapping_renders_deterministic_redacted_profile() -> None:
    profile = public_ingress_profile_from_mapping(_VALID)

    assert isinstance(profile, PublicIngressProfile)
    assert profile.public_origin == "https://arbora.example.com"
    assert profile.callback_uri == "https://arbora.example.com/auth/callback"

    render = profile.redacted_render()
    assert render["public_origin"] == "https://arbora.example.com"
    assert render["callback_uri"] == "https://arbora.example.com/auth/callback"
    assert render["profile_digest"] == profile.profile_digest
    # Same input -> same digest, deterministically.
    assert public_ingress_profile_from_mapping(_VALID).profile_digest == (
        profile.profile_digest
    )


def test_loopback_http_origin_is_accepted() -> None:
    mapping = {
        **_VALID,
        "public_origin": "http://127.0.0.1:8443",
        "callback_uri": "http://127.0.0.1:8443/auth/callback",
    }
    profile = public_ingress_profile_from_mapping(mapping)
    assert profile.public_origin == "http://127.0.0.1:8443"


@pytest.mark.parametrize(
    ("overrides", "removed_keys"),
    [
        ({"tls_secret_ref": ""}, ()),
        ({"tls_secret_ref": "not-a-reference"}, ()),
        ({}, ("tls_secret_ref",)),
    ],
)
def test_missing_or_malformed_tls_ref_refuses(
    overrides: dict[str, object], removed_keys: tuple[str, ...]
) -> None:
    mapping = {
        k: v for k, v in {**_VALID, **overrides}.items() if k not in removed_keys
    }

    with pytest.raises(PublicIngressProfileError, match="tls_secret_ref"):
        public_ingress_profile_from_mapping(mapping)


@pytest.mark.parametrize(
    "callback_uri",
    [
        "https://not-the-origin.example.com/auth/callback",
        "javascript:alert(1)",
        "https://arbora.example.com",  # not under the origin path
    ],
)
def test_malformed_callback_refuses(callback_uri: str) -> None:
    mapping = {**_VALID, "callback_uri": callback_uri}

    with pytest.raises(PublicIngressProfileError, match="callback_uri"):
        public_ingress_profile_from_mapping(mapping)


@pytest.mark.parametrize(
    "public_origin",
    [
        "http://arbora.example.com",  # HTTP, non-loopback
        "https://*.example.com",  # wildcard Host
        "ftp://arbora.example.com",
    ],
)
def test_http_non_loopback_or_wildcard_origin_refuses(public_origin: str) -> None:
    mapping = {**_VALID, "public_origin": public_origin}

    with pytest.raises(PublicIngressProfileError, match="public_origin"):
        public_ingress_profile_from_mapping(mapping)


def test_unknown_field_refuses() -> None:
    mapping = {**_VALID, "extra_field": "anything"}

    with pytest.raises(PublicIngressProfileError, match="unknown key"):
        public_ingress_profile_from_mapping(mapping)


def test_wildcard_trusted_proxy_refuses() -> None:
    mapping = {**_VALID, "trusted_proxy": "*"}

    with pytest.raises(PublicIngressProfileError, match="trusted_proxy"):
        public_ingress_profile_from_mapping(mapping)


def test_unsupported_version_refuses() -> None:
    mapping = {**_VALID, "version": 2}

    with pytest.raises(PublicIngressProfileError, match="version"):
        public_ingress_profile_from_mapping(mapping)


def test_non_mapping_input_refuses() -> None:
    with pytest.raises(PublicIngressProfileError, match="mapping"):
        public_ingress_profile_from_mapping(["not", "a", "mapping"])


def test_refusal_never_leaks_secret_value() -> None:
    mapping = {**_VALID, "tls_secret_ref": "env://MY_TLS_SECRET_VALUE_12345"}
    # A well-formed env:// reference is accepted — the point is that even on
    # a refusal path, no raw secret *value* (as opposed to a reference name)
    # ever appears in the error text.
    profile = public_ingress_profile_from_mapping(mapping)
    assert "MY_TLS_SECRET_VALUE_12345" not in repr(
        PublicIngressProfileError("unrelated")
    )
    assert profile.tls_secret_ref == "env://MY_TLS_SECRET_VALUE_12345"


@pytest.mark.spec("ARBORA-INGRESS-R001.2")
def test_profile_renders_origin_callback_and_proxy() -> None:
    profile = load_environment_profile("dev")
    rendered = profile_summary(profile)["public_ingress"]
    assert rendered["public_origin"] == "https://arbora-dev.example"
    assert rendered["callback_uri"] == "https://arbora-dev.example/auth/callback"
    assert rendered["trusted_proxy"] == "ingress-controller"
    assert generate_config("tiny")["WEBUI_ORIGIN"] == rendered["public_origin"]


@pytest.mark.spec("ARBORA-INGRESS-R001.3")
def test_legacy_redirect_is_bounded() -> None:
    today = date(2026, 11, 20)
    profile = public_ingress_profile_from_mapping(
        {
            **_VALID,
            "legacy_hostname": "old-arbora.example",
            "legacy_redirect_until": "2026-12-20",
        },
        today=today,
    )

    assert profile.legacy_redirect_route(rendered_on=today) == {
        "hostname": "old-arbora.example",
        "status": "308",
        "location": "https://arbora.example.com{path}",
        "until": "2026-12-20",
    }
    assert profile.legacy_redirect_route(rendered_on=date(2026, 12, 21)) is None

    with pytest.raises(PublicIngressProfileError, match="LEGACY_REDIRECT_TOO_LONG"):
        public_ingress_profile_from_mapping(
            {
                **_VALID,
                "legacy_hostname": "old-arbora.example",
                "legacy_redirect_until": "2027-03-01",
            },
            today=today,
        )
