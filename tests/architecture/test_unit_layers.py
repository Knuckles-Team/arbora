"""The arbora package units follow the layer order in ``arbora/layers.toml``.

ARBORA-HOST-R038: a unit (a top-level module or package under ``arbora/``)
imports only its own layer or an earlier one, the unit import graph has no
cycle, and no unit reaches a leading-underscore module or name of another
unit. Function-local imports count: they are coupling a cold import hides.
"""

from __future__ import annotations

import ast
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.spec("ARBORA-HOST-R038")

PACKAGE = Path(__file__).resolve().parents[2] / "arbora"


@dataclass(frozen=True)
class Finding:
    rule: str
    location: str
    detail: str


def load_layers(path: Path) -> list[tuple[str, tuple[str, ...]]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return [(layer["name"], tuple(layer["units"])) for layer in data["layer"]]


def package_units(package: Path) -> set[str]:
    units = set()
    for child in package.iterdir():
        if child.name == "__pycache__":
            continue
        if child.is_dir() and any(child.rglob("*.py")):
            units.add(child.name)
        elif child.suffix == ".py":
            units.add(child.stem)
    return units


def _is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _module_name(package: Path, path: Path) -> tuple[list[str], bool]:
    parts = [package.name, *path.relative_to(package).with_suffix("").parts]
    if parts[-1] == "__init__":
        return parts[:-1], True
    return parts, False


def _imports(package: Path, path: Path) -> Iterator[tuple[int, list[str], str | None]]:
    """Yield ``(line, dotted target, imported name)`` for every arbora import."""
    module, is_package = _module_name(package, path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split("."), None
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = module if is_package else module[:-1]
                base = base[: len(base) - (node.level - 1)]
                target = [*base, *(node.module.split(".") if node.module else [])]
            else:
                target = (node.module or "").split(".")
            for alias in node.names:
                yield node.lineno, target, alias.name


def scan(package: Path, layers: list[tuple[str, tuple[str, ...]]]) -> list[Finding]:
    units = package_units(package)
    rank: dict[str, int] = {}
    findings: list[Finding] = []
    for index, (name, members) in enumerate(layers):
        for unit in members:
            if unit in rank:
                findings.append(
                    Finding(
                        "duplicate-unit",
                        "arbora/layers.toml",
                        f"{unit} is in two layers",
                    )
                )
            rank[unit] = index
    for unit in sorted(units - rank.keys()):
        findings.append(
            Finding("unlayered-unit", f"arbora/{unit}", f"{unit} is named in no layer")
        )
    for unit in sorted(rank.keys() - units):
        findings.append(
            Finding(
                "unknown-unit",
                "arbora/layers.toml",
                f"{unit} is not a unit of the package",
            )
        )
    edges: dict[str, set[str]] = {unit: set() for unit in units}
    for path in sorted(package.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.relative_to(package).parts[0].removesuffix(".py")
        where = path.relative_to(package.parent).as_posix()
        seen: set[tuple[int, str]] = set()
        for line, target, name in _imports(package, path):
            if target[0] != package.name:
                continue
            if len(target) == 1:
                if name is None or name not in units:
                    continue
                target = [package.name, name]
                name = None
            unit = target[1]
            if unit == source or unit not in units:
                continue
            edges[source].add(unit)
            dotted = ".".join(target)
            if (
                rank.get(unit, -1) > rank.get(source, len(layers))
                and (line, dotted) not in seen
            ):
                seen.add((line, dotted))
                findings.append(
                    Finding("upward-import", f"{where}:{line}", f"{source} -> {dotted}")
                )
            if any(_is_private(part) for part in target[1:]) or (
                name and _is_private(name)
            ):
                shown = f"{dotted}.{name}" if name else dotted
                findings.append(
                    Finding("private-reach", f"{where}:{line}", f"{source} -> {shown}")
                )
    findings.extend(_cycles(edges))
    return findings


def _cycles(edges: dict[str, set[str]]) -> Iterator[Finding]:
    """Tarjan's strongly connected components, reported by unit."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    groups: list[list[str]] = []

    def visit(unit: str) -> None:
        index[unit] = low[unit] = len(index)
        stack.append(unit)
        on_stack.add(unit)
        for other in sorted(edges[unit]):
            if other not in index:
                visit(other)
                low[unit] = min(low[unit], low[other])
            elif other in on_stack:
                low[unit] = min(low[unit], index[other])
        if low[unit] == index[unit]:
            group = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                group.append(member)
                if member == unit:
                    break
            if len(group) > 1:
                groups.append(sorted(group))

    for unit in sorted(edges):
        if unit not in index:
            visit(unit)
    for group in groups:
        yield Finding("import-cycle", f"arbora/{group[0]}", " <-> ".join(group))


def test_every_unit_is_named_in_exactly_one_layer() -> None:
    findings = scan(PACKAGE, load_layers(PACKAGE / "layers.toml"))
    misnamed = [
        f
        for f in findings
        if f.rule in {"duplicate-unit", "unlayered-unit", "unknown-unit"}
    ]
    assert misnamed == []


def _write(root: Path, relative: str, text: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_scan_reports_each_rule_with_its_location(tmp_path: Path) -> None:
    package = tmp_path / "arbora"
    _write(package, "__init__.py")
    _write(package, "core/__init__.py", "from arbora.host import thing\n")
    _write(package, "core/_hidden.py")
    _write(
        package,
        "mid.py",
        "from arbora.core import _hidden\nfrom arbora.core.settings import _secret\n",
    )
    _write(
        package,
        "host/__init__.py",
        "import arbora.core\nfrom . import peer\nthing = 1\n",
    )
    _write(package, "host/peer.py")
    _write(package, "loose.py")
    layers = [("base", ("core",)), ("middle", ("mid",)), ("top", ("host", "__init__"))]

    by_rule = {(f.rule, f.location, f.detail) for f in scan(package, layers)}

    assert (
        "upward-import",
        "arbora/core/__init__.py:1",
        "core -> arbora.host",
    ) in by_rule
    assert ("private-reach", "arbora/mid.py:1", "mid -> arbora.core._hidden") in by_rule
    assert (
        "private-reach",
        "arbora/mid.py:2",
        "mid -> arbora.core.settings._secret",
    ) in by_rule
    assert ("unlayered-unit", "arbora/loose", "loose is named in no layer") in by_rule
    assert ("import-cycle", "arbora/core", "core <-> host") in by_rule
    # A downward import and an import inside the own unit are not findings.
    assert not any(detail.startswith("host ->") for _, _, detail in by_rule)
