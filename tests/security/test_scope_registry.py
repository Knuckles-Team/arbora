"""arbora's identity scope registry is ingested from the scopes each server serves."""

from __future__ import annotations

import json

import pytest

from arbora.security import scope_registry
from arbora.security.scope_registry import OnboardingRefused, ScopeRegistry


def _entry(scope: str, scope_class: str, group: str | None = None) -> dict[str, object]:
    return {"scope": scope, "class": scope_class, "approver_group": group}


@pytest.mark.spec("ARBORA-IDENTITY-R002.1")
def test_the_registry_is_the_installed_engine_catalog_scopes() -> None:
    engine = pytest.importorskip("medulla.engine")
    served = {
        entry["scope"]: entry["class"]
        for entry in json.loads(engine.method_catalog())["scopes"]
    }
    classes = scope_registry.scope_classes()
    assert {name: classes[name] for name in served} == served
    assert set(served) <= scope_registry.session_scopes()


@pytest.mark.spec("ARBORA-IDENTITY-R002.1")
def test_the_session_allowlist_is_exactly_the_registry() -> None:
    assert frozenset(scope_registry.scope_classes()) == scope_registry.session_scopes()
    assert scope_registry.scope_classes()["rbac:approve-elevation"] == "approver"
    assert (
        scope_registry.approver_groups()["rbac:approve-elevation"]
        == "elevation-approvers"
    )
    assert set(scope_registry.scope_classes().values()) <= set(scope_registry.CLASSES)


@pytest.mark.spec("ARBORA-IDENTITY-R002.1")
def test_the_registry_refuses_an_unknown_scope_class() -> None:
    registry = ScopeRegistry()
    with pytest.raises(OnboardingRefused, match="unknown class"):
        registry.register("fixture", [_entry("x:y", "superuser")])
    assert registry.scopes() == frozenset()


@pytest.mark.spec("ARBORA-IDENTITY-R002.2")
def test_handshake_scopes_form_the_session_allowlist() -> None:
    registry = ScopeRegistry()
    registry.register(
        "engine",
        [_entry("a:read", "user"), _entry("a:approve", "approver", "approvers")],
    )
    registry.register(
        "connector-x", [_entry("x:read", "domain"), _entry("a:read", "user")]
    )
    assert registry.scopes() == {"a:read", "a:approve", "x:read"}
    assert registry.classes() == {
        "a:read": "user",
        "a:approve": "approver",
        "x:read": "domain",
    }
    assert registry.groups() == {"a:approve": "approvers"}
    assert not hasattr(registry.classes(), "__setitem__")


@pytest.mark.spec("ARBORA-IDENTITY-R002.2")
def test_a_class_conflict_refuses_the_server_and_registers_none_of_it() -> None:
    registry = ScopeRegistry()
    registry.register("engine", [_entry("a:read", "user")])
    with pytest.raises(OnboardingRefused) as refused:
        registry.register(
            "connector-x", [_entry("x:new", "user"), _entry("a:read", "admin")]
        )
    assert refused.value.server == "connector-x"
    assert registry.scopes() == {"a:read"}
    with pytest.raises(OnboardingRefused):
        registry.register(
            "connector-y", [_entry("y:z", "user"), _entry("y:z", "admin")]
        )
    assert registry.scopes() == {"a:read"}


@pytest.mark.spec("ARBORA-IDENTITY-R002.2")
def test_a_served_connector_scope_reaches_the_session_allowlist_and_a_conflict_refuses() -> (
    None
):
    pytest.importorskip("medulla.engine")
    scope_registry.register_served_scopes(
        "fixture-connector", [_entry("fixture:r002-read", "user")]
    )
    assert "fixture:r002-read" in scope_registry.session_scopes()
    with pytest.raises(OnboardingRefused):
        scope_registry.register_served_scopes(
            "other-connector", [_entry("fixture:r002-read", "admin")]
        )


@pytest.mark.spec("ARBORA-IDENTITY-R002.2")
def test_no_packaged_scope_file_or_generator_remains() -> None:
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert not (root / "scripts" / "gen_scope_registry.py").exists()
    source = (root / "arbora" / "security" / "scope_registry.py").read_text("utf-8")
    assert "scopes.json" not in source
    listed = subprocess.run(
        ["git", "ls-files", "arbora/security"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert "scopes.json" not in listed


@pytest.mark.spec("ARBORA-IDENTITY-R020")
def test_exported_domain_scopes_match_the_registry() -> None:
    expected = {
        "finance:read": "domain",
        "fleet:read": "user",
        "fleet:control": "admin",
        "loops:read": "user",
        "loops:control": "admin",
        "ops:read": "admin",
        "ops:admin": "admin",
    }
    classes = scope_registry.scope_classes()
    assert {name: classes.get(name) for name in expected} == expected
    # One registry: every exported domain scope is a session scope too.
    assert set(expected) <= scope_registry.session_scopes()
