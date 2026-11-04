"""Registry property test for ARBORA-IDENTITY-R002.

Arbora's own domain scopes must use the shared scope classes and stay
consistent with the single generated engine scope registry rather than a
separately maintained list. This asserts every scope Arbora declares in
``arbora.api.policy.domain_scopes`` matches a fixture contract's name and
class when they agree, and that a declaration drifting from the registry
(wrong class, or a name the registry no longer carries) is caught rather
than silently accepted. The fixture is a small in-memory method catalog in
the shape ``medulla.engine.method_catalog()`` serves, mirroring
``tests/api/test_eg_binding.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arbora.api.policy.domain_scopes import (
    ARBORA_DOMAIN_SCOPES,
    SCOPE_CLASSES,
    DomainScope,
    generated_registry_mismatches,
    scopes_by_name,
)
from tests.api._catalog import scope_row
from tests.api.test_eg_binding import _fixture


def test_every_declared_scope_uses_a_shared_scope_class() -> None:
    for scope in ARBORA_DOMAIN_SCOPES:
        assert scope.scope_class in SCOPE_CLASSES


def test_rejects_an_unknown_scope_class() -> None:
    with pytest.raises(ValueError, match="unknown scope class"):
        DomainScope("fleet:events", "superadmin")


def test_scopes_by_name_indexes_every_declared_scope() -> None:
    indexed = scopes_by_name()
    assert set(indexed) == {scope.name for scope in ARBORA_DOMAIN_SCOPES}
    assert indexed["webui:admin"].scope_class == "admin"


@pytest.mark.spec("ARBORA-IDENTITY-R002")
def test_declared_scopes_match_a_consistent_registry(tmp_path: Path) -> None:
    document, _exclusions = _fixture(tmp_path)
    document["scopes"].extend(
        scope_row(scope.name, scope.scope_class) for scope in ARBORA_DOMAIN_SCOPES
    )

    mismatches = generated_registry_mismatches(catalog=document)

    assert mismatches == {}


def test_a_class_drift_from_the_registry_is_reported(tmp_path: Path) -> None:
    document, _exclusions = _fixture(tmp_path)
    document["scopes"].append(scope_row("fleet:events", "admin"))

    mismatches = generated_registry_mismatches(catalog=document)

    assert "fleet:events" in mismatches
    assert "service-only" in mismatches["fleet:events"]
    assert "admin" in mismatches["fleet:events"]


def test_a_scope_missing_from_the_registry_is_reported(tmp_path: Path) -> None:
    document, _exclusions = _fixture(tmp_path)

    mismatches = generated_registry_mismatches(catalog=document)

    assert mismatches == {
        scope.name: "missing from the generated scope registry"
        for scope in ARBORA_DOMAIN_SCOPES
    }
