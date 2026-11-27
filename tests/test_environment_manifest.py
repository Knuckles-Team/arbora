"""The host environment manifest, its census and startup validation (ARBORA-HOST-R010.2 to R010.8)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from arbora.core import environment_manifest as em
from arbora.core.environment_manifest import (
    EnvironmentDeclaration,
    EnvironmentRefusal,
    HostEnvironmentManifest,
)

ROOT = Path(__file__).resolve().parents[1]


def _decl(
    name: str, component: str = "arbora", **over: object
) -> EnvironmentDeclaration:
    base: dict[str, object] = {
        "name": name,
        "component": component,
        "type": "string",
        "required_when": "optional",
        "default": None,
        "sensitive": False,
        "call_sites": (f"{component}.site",),
    }
    base.update(over)
    return EnvironmentDeclaration(**base)  # type: ignore[arg-type]


def _manifest(*items: EnvironmentDeclaration) -> HostEnvironmentManifest:
    return HostEnvironmentManifest(1, "arbora", "", tuple(items))


def _owner(*names: str) -> tuple[str, em._Export]:
    items = [
        SimpleNamespace(
            name=n,
            type="string",
            required_when=None,
            default=None,
            sensitive=False,
            call_sites=("owner.site",),
        )
        for n in names
    ]
    return ("fake", lambda: (items, "1"))


def _without_slots(names: set[str]) -> set[str]:
    return {name for name in names if "{" not in name}


@pytest.mark.spec("ARBORA-HOST-R010.2")
def test_census_matches_declarations_and_dynamic_read_refuses(tmp_path: Path) -> None:
    declared = {d.name for d in em.arbora_declarations()}
    em.check_census(em.package_census(), declared)
    unused = {name for name, _k, _s in em._OTHER_READS} - {
        r.name for r in em.package_census()
    }
    # A family declared with a slot is read under names built at the call.
    unused = _without_slots(unused)
    assert unused == set()
    tree = tmp_path / "arbora"
    tree.mkdir()
    (tree / "leak.py").write_text(
        "import os\n\ndef read(key):\n    return os.environ.get(key)\n"
        "\ndef other():\n    return os.environ.get('UNDECLARED_NAME_X')\n",
        encoding="utf-8",
    )
    with pytest.raises(EnvironmentRefusal) as raised:
        em.check_census(em.census(tree), declared)
    assert raised.value.code == "ENV_UNDECLARED_READ"
    assert raised.value.names == ("arbora/leak.py:4", "arbora/leak.py:7")


@pytest.mark.spec("ARBORA-HOST-R010.2")
def test_manifest_holds_every_settings_alias_and_digests_without_values() -> None:
    manifest = em.load_environment_manifest(owners=[_owner("OWNER_NAME")])
    names = manifest.names()
    assert {"DEPLOYMENT_PROFILE", "OWNER_NAME"} <= names
    again = em.load_environment_manifest(owners=[_owner("OWNER_NAME")])
    assert manifest.digest == again.digest
    assert len(manifest.digest) == 64


@pytest.mark.spec("ARBORA-HOST-R010.2")
def test_a_name_declared_with_two_shapes_refuses_as_a_conflict() -> None:
    clash = SimpleNamespace(
        name="DEPLOYMENT_PROFILE",
        type="int",
        required_when=None,
        default=None,
        sensitive=False,
        call_sites=("x",),
    )
    with pytest.raises(EnvironmentRefusal) as raised:
        em.load_environment_manifest(owners=[("fake", lambda: ([clash], "1"))])
    assert raised.value.code == "ENV_DECLARATION_CONFLICT"
    assert raised.value.names == ("DEPLOYMENT_PROFILE",)


@pytest.mark.spec("ARBORA-HOST-R010.4")
def test_disabled_component_needs_no_credential() -> None:
    manifest = _manifest(
        _decl("FEATURE_ENABLED", type="bool"),
        _decl(
            "FEATURE_TOKEN",
            "feature",
            required_when="when:FEATURE_ENABLED",
            type="secret_reference",
            sensitive=True,
        ),
    )
    em.validate_startup({}, manifest=manifest)
    with pytest.raises(EnvironmentRefusal) as raised:
        em.validate_startup({"FEATURE_ENABLED": "true"}, manifest=manifest)
    assert raised.value.code == "CONFIG_REQUIRED_MISSING"
    assert raised.value.names == ("feature:FEATURE_TOKEN",)
    ok = em.validate_startup(
        {"FEATURE_ENABLED": "true", "FEATURE_TOKEN": "env://TOKEN"}, manifest=manifest
    )
    assert ("FEATURE_TOKEN", "set") in ok.settings


@pytest.mark.spec("ARBORA-HOST-R010.4")
def test_unresolved_condition_and_invalid_default_refuse_without_selecting_a_profile() -> (
    None
):
    dangling = _manifest(_decl("X_TOKEN", required_when="when:NOT_DECLARED"))
    with pytest.raises(EnvironmentRefusal) as raised:
        em.validate_startup({}, manifest=dangling)
    assert raised.value.code == "CONFIG_CONDITION_UNRESOLVED"
    bad = _manifest(_decl("X_COUNT", type="int", default="many"))
    with pytest.raises(EnvironmentRefusal) as raised:
        em.validate_startup({}, manifest=bad)
    assert raised.value.code == "CONFIG_DEFAULT_INVALID"
    assert raised.value.names == ("X_COUNT",)


@pytest.mark.spec("ARBORA-HOST-R010.3")
def test_each_refusal_names_the_component_and_variable_never_the_value() -> None:
    manifest = _manifest(
        _decl("NEEDED", "svc", required_when="always"),
        _decl("COUNT", "svc", type="int"),
    )
    with pytest.raises(EnvironmentRefusal) as missing:
        em.validate_startup({}, manifest=manifest)
    assert missing.value.code == "CONFIG_REQUIRED_MISSING"
    assert missing.value.names == ("svc:NEEDED",)
    with pytest.raises(EnvironmentRefusal) as malformed:
        em.validate_startup(
            {"NEEDED": "x", "COUNT": "secret-text-9"}, manifest=manifest
        )
    assert malformed.value.code == "CONFIG_MALFORMED"
    assert "secret-text-9" not in str(malformed.value)
    with pytest.raises(EnvironmentRefusal) as profile:
        em.validate_startup(
            {"DEPLOYMENT_PROFILE": "no-such-profile"}, owners=[_owner("A")]
        )
    assert profile.value.code == "CONFIG_PROFILE_UNKNOWN"
    assert profile.value.names == ("arbora:DEPLOYMENT_PROFILE",)


@pytest.mark.spec("ARBORA-HOST-R010.5")
def test_documented_examples_cover_manifest() -> None:
    real = _manifest(*em.arbora_declarations())
    docs = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert len(em.check_guidance(real, docs, readme)) == 64
    required = _manifest(_decl("MUST_BE_SHOWN", required_when="always"))
    assert em.example_names(required) == ("MUST_BE_SHOWN",)
    with pytest.raises(EnvironmentRefusal) as raised:
        em.check_guidance(required, "export OTHER_NAME=1")
    assert raised.value.code == "GUIDANCE_NAME_MISSING"
    assert raised.value.names == ("MUST_BE_SHOWN",)
    assert em.check_guidance(required, "export MUST_BE_SHOWN=<secret>")


@pytest.mark.spec("ARBORA-HOST-R010.8")
def test_unavailable_component_manifest_prevents_startup() -> None:
    def absent() -> tuple[list[object], str]:
        raise RuntimeError("UNAVAILABLE: the pinned release lacks the export")

    with pytest.raises(EnvironmentRefusal) as raised:
        em.validate_startup({}, owners=[("scion-sdk", absent)])
    assert raised.value.code == "ENV_MANIFEST_UNAVAILABLE"
    assert raised.value.names == ("scion-sdk",)
    assert raised.value.pin_lag is True
    with pytest.raises(EnvironmentRefusal) as empty:
        em.validate_startup({}, owners=[("meristem", lambda: ([], "1"))])
    assert empty.value.code == "ENV_MANIFEST_UNAVAILABLE"
    assert empty.value.pin_lag is False


@pytest.mark.spec("ARBORA-HOST-R010.8")
def test_owner_exports_compose_without_arbora_adding_their_names() -> None:
    manifest = em.load_environment_manifest(owners=[_owner("OWNER_ONLY")])
    owned = [d for d in manifest.declarations if d.name == "OWNER_ONLY"]
    assert [d.component for d in owned] == ["fake"]
    assert "OWNER_ONLY" not in {d.name for d in em.arbora_declarations()}


@pytest.mark.spec("ARBORA-HOST-R010.8")
def test_the_pinned_release_names_the_missing_owner_export() -> None:
    from arbora.pin_lag import meristem, scion_sdk

    try:
        scion_sdk.require_environment_declarations()
    except Exception as exc:
        assert exc.missing == scion_sdk.ENVIRONMENT_SYMBOL  # type: ignore[attr-defined]
    try:
        meristem.environment_declarations()
    except Exception as exc:
        assert "environment_declarations" in exc.missing  # type: ignore[attr-defined]
