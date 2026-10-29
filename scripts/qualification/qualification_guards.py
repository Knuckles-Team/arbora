"""Fail-closed guards for reviewed installed-only restart qualification."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata as metadata
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def preflight(args: list[str]) -> None:
    binding, wheels, bundle, tuig, manifest_path, target = map(Path, args)
    for path in (binding, wheels, bundle, tuig, manifest_path, target):
        require(path.is_absolute(), f"absolute path required: {path}")
        require(not path.is_symlink(), f"symlink not allowed: {path}")
    require(
        not target.exists(), f"fresh nonexistent qualification root required: {target}"
    )
    b = json.loads(binding.read_text())
    for name in ("arbora", "scion"):
        require(
            bool(re.fullmatch("[0-9a-f]{40}", b[name]["candidate_commit"] or "")),
            f"{name}: reviewed published candidate commit required",
        )
    selected = []
    for pattern, expected in (
        ("medulla-*.whl", b["producer"]["client_wheel_sha256"]),
        ("medulla_stem-*.whl", b["producer"]["server_wheel_sha256"]),
        ("arbora-*.whl", b["arbora"]["local_wheel_sha256"]),
        ("scion_sdk-*.whl", b["scion"]["local_wheel_sha256"]),
        ("meristem-*.whl", b["meristem"]["wheel_sha256"]),
    ):
        files = list(wheels.glob(pattern))
        require(len(files) == 1, f"exactly one reviewed wheel required: {pattern}")
        require(
            not files[0].is_symlink() and files[0].is_file(),
            f"regular wheel required: {pattern}",
        )
        require(digest(files[0]) == expected, f"wheel hash mismatch: {pattern}")
        selected.extend(files)
    require(set(wheels.glob("*.whl")) == set(selected), "unreviewed additional wheel")
    require(digest(tuig) == b["tuig"]["binary_sha256"], "Tuig binary hash mismatch")
    require(
        digest(manifest_path) == b["restart_bundle_manifest_sha256"],
        "bundle manifest hash mismatch",
    )
    manifest = json.loads(manifest_path.read_text())
    all_paths = list(bundle.rglob("*"))
    require(not any(p.is_symlink() for p in all_paths), "bundle symlink refused")
    actual = {str(p.relative_to(bundle)) for p in all_paths if p.is_file()}
    require(actual == set(manifest), "bundle file set differs from reviewed manifest")
    for name, expected in manifest.items():
        path = (bundle / name).resolve()
        require(path.is_relative_to(bundle.resolve()), "bundle traversal refused")
        require(digest(path) == expected, f"bundle hash mismatch: {name}")
    require(
        b["compatibility"]["blocked_closure_count"] == 35,
        "compatibility refusals may not be waived",
    )
    target.mkdir(parents=True, exist_ok=False)
    (target / "evidence").mkdir()
    (target / "requirements.in").write_text(
        "\n".join(str(p) for p in sorted(selected))
        + "\npytest==9.1.1\npytest-asyncio==1.4.0\n"
    )


def installed(binding_path: str) -> None:
    b = json.loads(Path(binding_path).read_text())
    prefix = Path(sys.prefix).resolve()
    pairs = (
        ("arbora", "arbora"),
        ("medulla", "epistemic_graph"),
        ("medulla-stem", "epistemic_graph_server"),
        ("meristem", "agent_utilities"),
        ("scion-sdk", "agent_connector_sdk"),
    )
    result = {}
    for distribution, module_name in pairs:
        module = importlib.import_module(module_name)
        require(
            bool(module.__file__), f"concrete module origin required: {module_name}"
        )
        origin = Path(module.__file__).resolve()
        require(origin.is_relative_to(prefix), f"non-installed module origin: {origin}")
        result[distribution] = {
            "version": metadata.version(distribution),
            "origin": str(origin),
        }
    for name in (
        "epistemic-graph",
        "epistemic-graph-server",
        "agent-utilities",
        "agent-connector-sdk",
    ):
        try:
            metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        raise SystemExit(f"retired overlapping distribution: {name}")
    from epistemic_graph.server_binary import resolve_server_binary

    binary = Path(resolve_server_binary()).resolve()
    require(binary.is_relative_to(prefix), f"non-installed server binary: {binary}")
    require(
        digest(binary) == b["producer"]["server_binary_sha256"],
        "installed server binary mismatch",
    )
    result["server_binary"] = {"path": str(binary), "sha256": digest(binary)}
    print(json.dumps(result, indent=2))


def junit(path: str) -> None:
    root = ET.parse(path).getroot()
    cases = root.findall(".//testcase")
    expected = {
        f"test_completed_turn_survives_both_process_restarts[{kind}]"
        for kind in ("overstory-wire", "tuig")
    }
    require(len(cases) == 2, "exactly two test cases required")
    require(
        {c.get("name") for c in cases} == expected, "unexpected test case identities"
    )
    require(
        all(
            c.get("classname") == "tests.qualification.test_native_history_restart"
            for c in cases
        ),
        "unexpected test class identities",
    )
    require(
        next(root.iter("failure"), None) is None
        and next(root.iter("error"), None) is None
        and next(root.iter("skipped"), None) is None,
        "failure/error/skip in qualification report",
    )


if __name__ == "__main__":
    mode, *args = sys.argv[1:]
    if mode == "preflight":
        preflight(args)
    elif mode == "installed":
        installed(*args)
    elif mode == "junit":
        junit(*args)
    else:
        raise SystemExit("unknown guard mode")
