"""Every class Arbora's packs declare extends an engine core class (ARBORA-DEPLOY-R020.1)."""

from __future__ import annotations

from pathlib import Path

import medulla
import pyoxigraph as ox
import pytest

import arbora

_RDFS_SUBCLASS = ox.NamedNode("http://www.w3.org/2000/01/rdf-schema#subClassOf")
_RDF_TYPE = ox.NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")
_CLASSES = (
    ox.NamedNode("http://www.w3.org/2002/07/owl#Class"),
    ox.NamedNode("http://www.w3.org/2000/01/rdf-schema#Class"),
)
_CORE_IRI_BASE = "https://knuckles-team.github.io/ontology/core#"


def _store(paths: list[Path]) -> ox.Store:
    store = ox.Store()
    for path in paths:
        store.load(path=str(path), format=ox.RdfFormat.TURTLE)
    return store


def _classes(store: ox.Store) -> set[str]:
    return {
        str(quad.subject.value)
        for kind in _CLASSES
        for quad in store.quads_for_pattern(None, _RDF_TYPE, kind)
        if isinstance(quad.subject, ox.NamedNode)
    }


def _core_files() -> list[Path]:
    root = Path(medulla.__file__).resolve().parent / "ontology"
    return sorted(p for p in root.glob("*.ttl") if not p.name.endswith(".shapes.ttl"))


def _pack_files() -> list[Path]:
    root = Path(arbora.__file__).resolve().parent / "ontology"
    return sorted(root.glob("*.ttl"))


@pytest.mark.spec("ARBORA-DEPLOY-R020.1")
def test_every_pack_class_has_a_core_subclass_path() -> None:
    core_classes = _classes(_store(_core_files()))
    assert any(iri.startswith(_CORE_IRI_BASE) for iri in core_classes), (
        "the installed engine ships no core ontology under the core IRI base"
    )
    pack = _store(_pack_files())
    declared = _classes(pack)
    assert declared, "the pack declares no classes"

    supers: dict[str, set[str]] = {}
    for quad in pack.quads_for_pattern(None, _RDFS_SUBCLASS, None):
        if isinstance(quad.subject, ox.NamedNode) and isinstance(
            quad.object, ox.NamedNode
        ):
            supers.setdefault(quad.subject.value, set()).add(quad.object.value)

    def reaches_core(start: str) -> bool:
        seen: set[str] = set()
        frontier = [start]
        while frontier:
            node = frontier.pop()
            if node in core_classes:
                return True
            if node not in seen:
                seen.add(node)
                frontier.extend(supers.get(node, ()))
        return False

    orphans = sorted(
        iri for iri in declared if iri not in core_classes and not reaches_core(iri)
    )
    assert not orphans, f"{len(orphans)} class(es) have no core path: {orphans[:10]}"
    # A pack never redeclares an engine class.
    assert not sorted(declared & core_classes)
