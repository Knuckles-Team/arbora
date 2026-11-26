"""ARBORA-FLEET-R006.1 to R006.4: the harvest call-site inventory, now zero.

``arbora/fleet/multiplexer.py`` no longer reads a skill or prompt body back
over the wire from a child. The resident local-skill catalog
(``arbora/fleet/local_skill_catalog.py``) is the only source of a body. This
static, source-level census scans the module for every harvest-named symbol
and pins the removal: the helpers, the specs and the budget constants are
gone, and the settled-probe fold carries a neutral name.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

import arbora

_MULTIPLEXER_RELATIVE_PATH = Path("fleet") / "multiplexer.py"

#: ARBORA-FLEET-R006.3 removed these symbols.
_REMOVED_SYMBOLS = frozenset(
    {
        "_harvest_resource_bodies",
        "_resolve_via_local_catalog_or_harvest",
        "_harvest_deadline",
        "_HARVEST_DEADLINE_SHARE",
        "_harvest_error_reason",
        "_SKILL_HARVEST_SPEC",
        "_PROMPT_HARVEST_SPEC",
        "_ResourceHarvestSpec",
        "_harvest_probe_results",
    }
)


def _multiplexer_source() -> str:
    path = Path(arbora.__file__).resolve().parent / _MULTIPLEXER_RELATIVE_PATH
    return path.read_text(encoding="utf-8")


def _defined_and_referenced_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _harvest_names(tree: ast.Module) -> set[str]:
    return {n for n in _defined_and_referenced_names(tree) if "harvest" in n.lower()}


@pytest.mark.spec("ARBORA-FLEET-R006.1")
def test_harvest_named_entry_points_match_pinned_baseline() -> None:
    """The pinned baseline after the removal is empty."""
    assert _harvest_names(ast.parse(_multiplexer_source())) == set()


@pytest.mark.spec("ARBORA-FLEET-R006.3")
@pytest.mark.spec("ARBORA-FLEET-R006.3.2")
def test_harvest_resource_bodies_absent() -> None:
    names = _defined_and_referenced_names(ast.parse(_multiplexer_source()))
    assert "_harvest_resource_bodies" not in names
    assert "_resolve_via_local_catalog_or_harvest" not in names


@pytest.mark.spec("ARBORA-FLEET-R006.3")
@pytest.mark.spec("ARBORA-FLEET-R006.3.3")
def test_deadline_helpers_absent() -> None:
    names = _defined_and_referenced_names(ast.parse(_multiplexer_source()))
    assert not names & {
        "_harvest_deadline",
        "_HARVEST_DEADLINE_SHARE",
        "_harvest_error_reason",
    }


@pytest.mark.spec("ARBORA-FLEET-R006.3")
@pytest.mark.spec("ARBORA-FLEET-R006.3.4")
def test_harvest_specs_absent() -> None:
    names = _defined_and_referenced_names(ast.parse(_multiplexer_source()))
    assert not names & {
        "_SKILL_HARVEST_SPEC",
        "_PROMPT_HARVEST_SPEC",
        "_ResourceHarvestSpec",
    }


@pytest.mark.spec("ARBORA-FLEET-R006.3")
def test_removed_symbols_absent() -> None:
    names = _defined_and_referenced_names(ast.parse(_multiplexer_source()))
    assert not names & _REMOVED_SYMBOLS


@pytest.mark.spec("ARBORA-FLEET-R006.4")
def test_no_harvest_named_symbol() -> None:
    tree = ast.parse(_multiplexer_source())
    assert not _harvest_names(tree)
    assert "_settled_probe_results" in _defined_and_referenced_names(tree)


@pytest.mark.spec("ARBORA-FLEET-R006.3.1")
def test_probe_protocol_families_has_no_direct_harvest_call() -> None:
    tree = ast.parse(_multiplexer_source())
    families = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_probe_protocol_families"
    )
    called = {
        call.func.attr
        for call in ast.walk(families)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    }
    assert "_fill_bodies_from_local_catalog" in called
    assert not {name for name in called if "harvest" in name}


@pytest.mark.spec("ARBORA-FLEET-R006.3.1")
@pytest.mark.asyncio
async def test_probe_protocol_families_resolves_admitted_skill_without_wire_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.fleet import multiplexer as mux

    resource = SimpleNamespace(
        uri="skill://admitted/SKILL.md", name="admitted", description="d"
    )

    class _Session:
        async def list_resources(self):
            return SimpleNamespace(resources=[resource])

        async def list_resource_templates(self):
            return SimpleNamespace(resource_templates=[])

        async def list_prompts(self):
            return SimpleNamespace(prompts=[])

        async def read_resource(self, *_a, **_k):
            raise AssertionError("wire read for an admitted child")

    monkeypatch.setattr(
        mux,
        "build_local_skill_catalog",
        lambda: ([{"name": "admitted", "instructions": "LOCAL BODY"}], []),
    )
    monkeypatch.setattr(
        mux,
        "_bounded_skill_catalog",
        lambda _r: [{"name": "admitted", "uri": resource.uri}],
    )
    monkeypatch.setattr(mux, "_bounded_prompt_catalog", lambda _r: [])
    monkeypatch.setattr(mux, "_bounded_descriptor_catalog", lambda *_a, **_k: [])
    m = mux.MCPMultiplexer.__new__(mux.MCPMultiplexer)
    *_rest, skills, _prompts, errors = await m._probe_protocol_families(
        "srv", _Session()
    )
    assert errors == {}
    assert skills[0]["instructions"] == "LOCAL BODY"
