"""arbora's identity scope registry is generated from the installed EG scopes.json."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from arbora.security import scope_registry

ROOT = Path(__file__).resolve().parents[2]


def _generator():
    spec = importlib.util.spec_from_file_location(
        "gen_scope_registry", ROOT / "scripts" / "gen_scope_registry.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.spec("ARBORA-IDENTITY-R002.1")
def test_the_generated_module_is_the_installed_eg_registry():
    pytest.importorskip("medulla")
    gen = _generator()
    assert (ROOT / "arbora" / "security" / "scope_registry.py").read_text(
        encoding="utf-8"
    ) == gen.render(gen.installed_registry_bytes())


@pytest.mark.spec("ARBORA-IDENTITY-R002.1")
def test_the_session_allowlist_is_exactly_the_registry():
    assert frozenset(scope_registry.SCOPE_CLASSES) == scope_registry.SESSION_SCOPES
    assert scope_registry.SCOPE_CLASSES["rbac:approve-elevation"] == "approver"
    assert (
        scope_registry.APPROVER_GROUPS["rbac:approve-elevation"]
        == "elevation-approvers"
    )


@pytest.mark.spec("ARBORA-IDENTITY-R002.1")
def test_the_generator_refuses_an_unknown_scope_class():
    bad = json.dumps({"scopes": [{"scope": "x:y", "class": "superuser"}]}).encode()
    with pytest.raises(ValueError, match="unknown scope classes"):
        _generator().render(bad)
