"""Bind same-run producer artifacts and immutable consumer builds; never promotion."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


def require(value: object, message: str) -> None:
    if not value:
        raise SystemExit(message)


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def one(path: Path, pattern: str) -> Path:
    paths = list(path.glob(pattern))
    require(
        len(paths) == 1 and paths[0].is_file() and not paths[0].is_symlink(),
        f"exact regular file required: {pattern}",
    )
    return paths[0]


def checked_hash(root: Path, name: str, digest: str, label: str) -> None:
    target = root / name
    require(target.resolve().is_relative_to(root.resolve()), f"{label} traversal")
    require(sha(target) == digest, f"{label} mismatch: {name}")


def verify_hash_file(manifest: Path, root: Path, label: str) -> None:
    for line in manifest.read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        checked_hash(root, name.lstrip("*"), digest, label)


def verify_sources(sources: Path, refs: dict[str, str]) -> dict:
    receipts = {}
    for name, ref in refs.items():
        require(
            bool(re.fullmatch("[0-9a-f]{40}", ref)), f"immutable {name} ref required"
        )
        path = sources / name
        require(git(path, "rev-parse", "HEAD") == ref, f"{name} checkout mismatch")
        require(
            not git(path, "status", "--porcelain", "--untracked-files=no"),
            f"{name} tracked source dirty",
        )
        receipts[name] = {"commit": ref, "tree": git(path, "rev-parse", "HEAD^{tree}")}
    return receipts


def verify_producer(
    root: Path, sources: Path, ref: str, tree: str
) -> tuple[Path, Path, dict]:
    producer, stem = root / "inputs/medulla", root / "inputs/stem"
    require(
        (producer / "source.sha").read_text().strip() == ref,
        "producer revision mismatch",
    )
    require(
        (producer / "source.tree").read_text().strip() == tree, "producer tree mismatch"
    )
    verify_hash_file(producer / "SHA256SUMS", producer, "producer hash")
    verify_hash_file(
        producer / "build-recipe.sha256", sources / "medulla", "producer recipe"
    )
    provenance = json.loads((stem / "provenance.json").read_text())
    require(
        provenance["source_revision"] == ref
        and provenance["source_tree"] == tree
        and provenance["source_dirty"] is False,
        "stem source mismatch",
    )
    require(
        provenance["installed_proof"]["verified"] is True,
        "stem installed smoke missing",
    )
    for name, digest in provenance["recipe_sha256"].items():
        checked_hash(sources / "medulla", name, digest, "stem recipe")
    client, server = one(producer, "medulla-*.whl"), one(stem, "medulla_stem-*.whl")
    require(
        provenance["artifact_sha256"] == {server.name: sha(server)},
        "stem artifact hash mismatch",
    )
    return client, server, provenance


def fetch_meristem(wheels: Path, expected: str) -> None:
    """Fetch only the reviewed released wheel; hash-check before use."""
    with urllib.request.urlopen(
        "https://pypi.org/pypi/meristem/4.0.0/json", timeout=30
    ) as response:
        metadata = json.load(response)
    chosen = [
        item
        for item in metadata["urls"]
        if item["digests"]["sha256"] == expected and item["filename"].endswith(".whl")
    ]
    require(len(chosen) == 1, "reviewed Meristem wheel unavailable")
    item = chosen[0]
    require(
        item["url"].startswith("https://files.pythonhosted.org/"),
        "unexpected wheel origin",
    )
    require(Path(item["filename"]).name == item["filename"], "wheel filename traversal")
    destination = wheels / item["filename"]
    with (
        urllib.request.urlopen(item["url"], timeout=60) as response,
        destination.open("wb") as out,
    ):
        shutil.copyfileobj(response, out)
    require(sha(destination) == expected, "Meristem wheel mismatch")


def bind_producer(
    bindings: dict, source: dict, client: Path, server: Path, provenance: dict
) -> None:
    bindings["status"] = "SAME_RUN_CANDIDATE_RUNTIME_EVIDENCE_NOT_RELEASE_ACCEPTANCE"
    bindings["producer"].update(
        head=source["commit"],
        tested_revision=source["commit"],
        tree=source["tree"],
        run_id=int(os.environ["GITHUB_RUN_ID"]),
        client_wheel_sha256=sha(client),
        server_wheel_sha256=sha(server),
        server_binary_sha256=provenance["installed_proof"]["binary_sha256"],
    )
    for field in ("client_artifact_id", "server_wheel_artifact_id"):
        bindings["producer"].pop(field, None)


def bind_consumers(bindings: dict, receipts: dict, wheels: Path, sources: Path) -> None:
    for name, pattern in [("arbora", "arbora-*.whl"), ("scion", "scion_sdk-*.whl")]:
        wheel = one(wheels, pattern)
        bindings[name]["candidate_commit"] = receipts[name]["commit"]
        bindings[name]["local_wheel_sha256"] = sha(wheel)
        receipts[name]["wheel"] = {"name": wheel.name, "sha256": sha(wheel)}
    binary = sources / "tuig/native/agent-terminal-ui/target/release/tuig"
    require(binary.is_file(), "native Tuig build absent")
    bindings["tuig"]["binary_sha256"] = sha(binary)
    bindings["tuig"]["verification_note"] = (
        "Built --locked --release from pinned source in this same read-only candidate job."
    )
    for field in ("validation_receipt_sha256", "binary_path"):
        bindings["tuig"].pop(field, None)
    bindings["tuig"]["validated_source"] = receipts["tuig"]["commit"]
    receipts["tuig"]["binary_sha256"] = sha(binary)


def prepare_bundle(root: Path, sources: Path, here: Path) -> None:
    bundle = root / "restart-bundle"
    bundle.mkdir(exist_ok=False)
    manifest = json.loads((here / "restart-bundle-manifest.json").read_text())
    for name, digest in manifest.items():
        source = (
            sources / "arbora" / name
            if name.startswith("tests/")
            else here / "bundle-scaffold" / name
        )
        target = bundle / name
        require(target.resolve().is_relative_to(bundle.resolve()), "bundle traversal")
        require(sha(source) == digest, f"reviewed bundle source mismatch: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def prepare(root: Path) -> None:
    sources, wheels = root / "sources", root / "wheelhouse"
    here = sources / "arbora/scripts/qualification"
    bindings = json.loads((here / "candidate-bindings.json").read_text())
    prior = copy.deepcopy(bindings)
    refs = {
        "arbora": os.environ["ARBORA_REF"],
        "scion": os.environ["SCION_REF"],
        "medulla": os.environ["PRODUCER_REF"],
        "tuig": bindings["tuig"]["candidate_commit"],
    }
    receipts = verify_sources(sources, refs)
    client, server, provenance = verify_producer(
        root, sources, refs["medulla"], receipts["medulla"]["tree"]
    )
    for wheel in (client, server):
        shutil.copy2(wheel, wheels / wheel.name)
    fetch_meristem(wheels, bindings["meristem"]["wheel_sha256"])
    bind_producer(bindings, receipts["medulla"], client, server, provenance)
    bind_consumers(bindings, receipts, wheels, sources)
    prepare_bundle(root, sources, here)
    receipts["producer"] = provenance
    receipts["prior_approved_candidate_bindings"] = prior
    (root / "runtime-bindings.json").write_text(json.dumps(bindings, indent=2) + "\n")
    (root / "candidate-build-receipt.json").write_text(
        json.dumps(receipts, indent=2) + "\n"
    )


if __name__ == "__main__":
    prepare(Path(sys.argv[1]).resolve())
