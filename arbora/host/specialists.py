"""Transactional specialist package install per provider root (ARBORA-HOST-R034).

A provider root holds immutable generation directories and one bounded marker file naming
the current generation. An install stages the package's assets, verifies every asset
SHA-256 against its declared digest, moves the staging directory to a new generation and
swaps the marker with one atomic rename. A digest mismatch refuses ``ASSET_DIGEST_MISMATCH``
and an unbounded marker refuses ``MARKER_UNBOUNDED``; neither changes the marker. A
successful install then registers ``cat:SpecialistPackage`` and ``cat:ProviderAsset`` nodes
through the engine ``AddTriples`` write. Discovery reads that catalog, never the filesystem.
If the registration fails, the marker returns to its previous generation.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from scion_sdk.ontology_namespaces import module_namespace

__all__ = [
    "MARKER_NAME",
    "MARKER_MAX_BYTES",
    "InstalledPackage",
    "ProviderAsset",
    "SpecialistInstallRefused",
    "SpecialistPackage",
    "catalog_ntriples",
    "current_generation",
    "install_package",
    "package_from_mapping",
]

MARKER_NAME = "CURRENT"
MARKER_MAX_BYTES = 4096
CAT = module_namespace("catalog")
CP = module_namespace("connector-pack")
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: ``AddTriples`` of one N-Triples document; the host passes the engine write.
Register = Callable[[str], Awaitable[object]]


class SpecialistInstallRefused(Exception):
    """An install refused with one closed code; the marker is unchanged."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProviderAsset:
    name: str
    sha256: str
    content: bytes


@dataclass(frozen=True, slots=True)
class SpecialistPackage:
    name: str
    version: str
    owner: str
    assets: Sequence[ProviderAsset]


@dataclass(frozen=True, slots=True)
class InstalledPackage:
    name: str
    version: str
    generation: str
    previous: str | None


def package_digest(package: SpecialistPackage) -> str:
    """The digest of the sorted ``(name, sha256)`` pairs of the package's assets."""
    body = "\n".join(f"{a.name}\0{a.sha256}" for a in sorted(package.assets, key=_key))
    return hashlib.sha256(body.encode()).hexdigest()


def _key(asset: ProviderAsset) -> str:
    return asset.name


def _generation(package: SpecialistPackage) -> str:
    return f"{package.name}-{package.version}-{package_digest(package)[:16]}"


def _check_names(package: SpecialistPackage) -> None:
    names = [package.name, package.version, *(a.name for a in package.assets)]
    if not all(_NAME.match(n) for n in names) or not package.assets:
        raise SpecialistInstallRefused("PACKAGE_INVALID")
    if len({a.name for a in package.assets}) != len(package.assets):
        raise SpecialistInstallRefused("PACKAGE_INVALID", "duplicate asset")
    if not package.owner.strip():
        raise SpecialistInstallRefused("PACKAGE_INVALID", "owner")
    if not all(_SHA256.match(a.sha256) for a in package.assets):
        raise SpecialistInstallRefused("PACKAGE_INVALID", "digest form")


def current_generation(root: Path) -> str | None:
    """The generation the marker names, or ``None``; an unbounded marker refuses."""
    marker = root / MARKER_NAME
    try:
        size = marker.stat().st_size
    except FileNotFoundError:
        return None
    if size > MARKER_MAX_BYTES:
        raise SpecialistInstallRefused("MARKER_UNBOUNDED")
    text = marker.read_text(encoding="utf-8").strip()
    return text or None


def _swap_marker(root: Path, generation: str | None) -> None:
    """Replace the marker with one atomic rename of a fully written file."""
    marker = root / MARKER_NAME
    if generation is None:
        marker.unlink(missing_ok=True)
        return
    staged = root / f".{MARKER_NAME}.{os.getpid()}.tmp"
    with staged.open("w", encoding="utf-8") as handle:
        handle.write(generation + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staged, marker)


def _stage(root: Path, package: SpecialistPackage, generation: str) -> Path:
    """Write the assets under a staging directory and verify each one from disk."""
    staging = root / f".staging-{generation}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        for asset in package.assets:
            (staging / asset.name).write_bytes(asset.content)
        for asset in package.assets:
            actual = hashlib.sha256((staging / asset.name).read_bytes()).hexdigest()
            if actual != asset.sha256:
                raise SpecialistInstallRefused("ASSET_DIGEST_MISMATCH", asset.name)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def package_iri(name: str) -> str:
    return f"urn:arbora:specialist:{quote(name, safe='')}"


def asset_iri(package: str, asset: str) -> str:
    return f"{package_iri(package)}:asset:{quote(asset, safe='')}"


def _literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def catalog_ntriples(package: SpecialistPackage) -> str:
    """The N-Triples that register the package and each provider asset in the catalog."""
    node = package_iri(package.name)
    rows: list[tuple[str, str, str]] = [
        (node, RDF_TYPE, f"<{CAT}SpecialistPackage>"),
        (node, RDFS_LABEL, _literal(package.name)),
        (node, f"{CP}packVersion", _literal(package.version)),
        (node, f"{CP}digest", _literal(package_digest(package))),
        (node, f"{CAT}owner", _literal(package.owner)),
    ]
    for asset in sorted(package.assets, key=_key):
        child = asset_iri(package.name, asset.name)
        rows += [
            (child, RDF_TYPE, f"<{CAT}ProviderAsset>"),
            (child, RDFS_LABEL, _literal(asset.name)),
            (child, f"{CP}digest", _literal(asset.sha256)),
            (child, f"{CAT}owner", _literal(package.owner)),
            (child, f"{CAT}partOf", f"<{node}>"),
        ]
    return "".join(f"<{s}> <{p}> {o} .\n" for s, p, o in rows)


async def install_package(
    root: Path, package: SpecialistPackage, *, register: Register
) -> InstalledPackage:
    """Install ``package`` under ``root`` as a new generation and register its catalog nodes.

    The marker changes only after every digest verified; ``register`` runs after the swap
    and a failure there puts the previous marker back.
    """
    _check_names(package)
    root.mkdir(parents=True, exist_ok=True)
    previous = current_generation(root)
    generation = _generation(package)
    target = root / "generations" / generation
    created = not target.exists()
    if created:
        staging = _stage(root, package, generation)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, target)
    try:
        _swap_marker(root, generation)
        try:
            await register(catalog_ntriples(package))
        except Exception:
            _swap_marker(root, previous)
            raise
    except BaseException:
        if created:
            shutil.rmtree(target, ignore_errors=True)
        raise
    return InstalledPackage(package.name, package.version, generation, previous)


def _asset_rows(value: object) -> Sequence[Mapping[str, object]]:
    """The wire ``assets`` value, checked to be a non-string sequence."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise TypeError("assets must be a sequence")
    return value


def package_from_mapping(body: Mapping[str, object]) -> SpecialistPackage:
    """A package from its wire form; asset ``content_hex`` is the asset's bytes in hex."""
    import binascii

    try:
        assets = [
            ProviderAsset(
                name=str(a["name"]),
                sha256=str(a["sha256"]),
                content=bytes.fromhex(str(a["content_hex"])),
            )
            for a in _asset_rows(body["assets"])
        ]
        return SpecialistPackage(
            name=str(body["name"]),
            version=str(body["version"]),
            owner=str(body["owner"]),
            assets=assets,
        )
    except (KeyError, TypeError, ValueError, binascii.Error):
        raise SpecialistInstallRefused("PACKAGE_INVALID") from None
