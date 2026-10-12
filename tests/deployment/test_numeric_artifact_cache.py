"""Cache acceptance and rebuild fixtures; no native compilation required."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/numeric_artifact_cache.py"
SPEC = importlib.util.spec_from_file_location("numeric_artifact_cache", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)


@pytest.fixture
def candidate(tmp_path: Path) -> tuple[Path, Path, str]:
    source, store = tmp_path / "source", tmp_path / "cache"
    (source / "epistemic_graph").mkdir(parents=True)
    store.mkdir()
    extension = store / "numeric.abi3.so"
    extension.write_bytes(b"fixture-extension")
    key = "numeric-v1-fixture"
    (store / "receipt.json").write_text(
        json.dumps(
            {"key": key, "artifact": extension.name, "sha256": cache.digest(extension)}
        )
    )
    return source, store, key


def test_hit_restores_the_receipted_artifact(candidate) -> None:
    source, store, key = candidate
    assert cache.restore(source, store, key)
    assert (
        source / "epistemic_graph/numeric.abi3.so"
    ).read_bytes() == b"fixture-extension"


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "identity",
        "digest",
        "traversal",
        "absolute",
        "abi",
        "symlink",
        "receipt",
    ],
)
def test_rejected_candidate_rebuilds(candidate, monkeypatch, failure) -> None:
    source, store, key = candidate
    receipt_path = store / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    if failure == "missing":
        receipt_path.unlink()
    elif failure in {"identity", "digest", "traversal", "absolute", "abi"}:
        field, value = {
            "identity": ("key", "other"),
            "digest": ("sha256", "bad"),
            "traversal": ("artifact", "../numeric.abi3.so"),
            "absolute": ("artifact", "/tmp/numeric.abi3.so"),
            "abi": ("artifact", "numeric.cpython-999.so"),
        }[failure]
        receipt[field] = value
        receipt_path.write_text(json.dumps(receipt))
    elif failure == "symlink":
        extension = store / receipt["artifact"]
        extension.unlink()
        extension.symlink_to(SCRIPT)
    elif failure == "receipt":
        receipt_path.write_text("[]")
    assert not cache.restore(source, store, key)
    cache.clear(source, store)
    assert not store.exists()
    assert not cache.extensions(source / "epistemic_graph")


@pytest.mark.parametrize(
    "outcome", ["hit", "miss", "rejected", "build-failure", "probe-failure"]
)
def test_workflow_rebuild_and_save_order(candidate, tmp_path, outcome) -> None:
    source, store, key = candidate
    if outcome != "hit":
        (store / "receipt.json").unlink()
    if outcome == "rejected":
        (store / "receipt.json").write_text(
            json.dumps(
                {
                    "key": key,
                    "artifact": "numeric.abi3.so",
                    "sha256": cache.digest(store / "numeric.abi3.so"),
                }
            )
        )
    runner = tmp_path / "python-probe"
    runner.write_text(
        f"#!{sys.executable}\n"
        + """
import os
import runpy
import sys
from pathlib import Path
args = sys.argv[1:]
if args[0] == "-I":
    args.pop(0)
if args[0].endswith("build_numeric_kernel.py"):
    if os.environ["OUTCOME"] == "build-failure":
        sys.exit(1)
    (Path(os.environ["eg_source"]) / "epistemic_graph/numeric.abi3.so").write_bytes(b"rebuilt")
    sys.exit(0)
if args[1] == "probe":
    data = (Path(args[2]) / "epistemic_graph/numeric.abi3.so").read_bytes()
    sys.exit(int(os.environ["OUTCOME"] == "probe-failure" or (os.environ["OUTCOME"] == "rejected" and data != b"rebuilt")))
sys.argv = args
runpy.run_path(args[0], run_name="__main__")
"""
    )
    runner.chmod(0o755)
    git = tmp_path / "git"
    git.write_text("#!/bin/sh\nexit 0\n")
    git.chmod(0o755)
    workflow = yaml.safe_load(
        (SCRIPT.parents[1] / ".github/workflows/release.yml").read_text()
    )
    step = next(
        step
        for step in workflow["jobs"]["gates"]["steps"]
        if step.get("name") == "Validate numeric extension or rebuild"
    )
    script = step["run"]
    script = (
        script[script.index("          helper=") :]
        if "          helper=" in script
        else script[script.index("helper=") :]
    )
    script = script[: script.index('echo "PYTHONPATH=')]
    cache_dir = tmp_path / "eg-numeric-artifact"
    store.rename(cache_dir)
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=SCRIPT.parents[1],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "PYO3_PYTHON": str(runner),
            "RUNNER_TEMP": str(tmp_path),
            "eg_source": str(source),
            "NUMERIC_CACHE_KEY": key,
            "OUTCOME": outcome,
        },
        capture_output=True,
        text=True,
    )
    if outcome.endswith("failure"):
        assert result.returncode != 0
        assert not (cache_dir / "receipt.json").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert (cache_dir / "receipt.json").exists()
        assert (source / "epistemic_graph/numeric.abi3.so").read_bytes() == (
            b"fixture-extension" if outcome == "hit" else b"rebuilt"
        )


@pytest.mark.parametrize("field", ["__file__", "__kernel__", "sum"])
def test_smoke_rejects_wrong_path_marker_or_result(
    candidate, monkeypatch, field
) -> None:
    source, store, key = candidate
    extension = store / "numeric.abi3.so"
    kernel = SimpleNamespace(
        __file__=str(extension), __kernel__="eg-numeric", sum=lambda values: 6.0
    )
    monkeypatch.setattr(cache.importlib, "import_module", lambda name: kernel)
    cache.smoke(source, extension)
    setattr(
        kernel,
        field,
        {"__file__": str(SCRIPT), "__kernel__": "other", "sum": lambda values: 7.0}[
            field
        ],
    )
    with pytest.raises(ValueError):
        cache.smoke(source, extension)


@pytest.mark.parametrize(
    "field",
    [
        "source",
        "rustc",
        "cargo",
        "maturin",
        "cc",
        "lock",
        "RUSTFLAGS",
        "CARGO_ENCODED_RUSTFLAGS",
        "CARGO_PROFILE_RELEASE_OPT_LEVEL",
        "PYO3_CONFIG_FILE",
        "ImageVersion",
        "python",
        "libc",
    ],
)
def test_identity_drift_changes_key(tmp_path, monkeypatch, field) -> None:
    lock = tmp_path / "Cargo.lock"
    lock.write_text("locked")
    manifest_list = tmp_path / "inputs"
    manifest_list.write_text("Cargo.lock")
    monkeypatch.setenv("NUMERIC_SOURCE_SHA", "a" * 40)
    monkeypatch.setenv("NUMERIC_TOOLS_ID", "toolchain-v1")
    if field == "PYO3_CONFIG_FILE":
        monkeypatch.delenv(field, raising=False)
    before = cache.cache_key(tmp_path, manifest_list)
    if field == "lock":
        lock.write_text("changed")
    elif field == "source":
        monkeypatch.setenv("NUMERIC_SOURCE_SHA", "b" * 40)
    elif field in {"rustc", "cargo", "maturin", "cc"}:
        monkeypatch.setenv("NUMERIC_TOOLS_ID", field + "-v2")
    elif field == "python":
        monkeypatch.setattr(cache.sys, "version", "other-python")
    elif field == "libc":
        monkeypatch.setattr(cache.platform, "libc_ver", lambda: ("other", "1"))
    elif field == "PYO3_CONFIG_FILE":
        monkeypatch.setenv(field, str(lock))
    else:
        monkeypatch.setenv(field, "changed")
    assert cache.cache_key(tmp_path, manifest_list) != before


@pytest.fixture
def cargo_tree(tmp_path, monkeypatch):
    cwd = tmp_path / "ancestor/parent/graphos"
    cwd.mkdir(parents=True)
    home = tmp_path / "cargo-home"
    home.mkdir()
    manifest_list = tmp_path / "inputs"
    manifest_list.write_text("")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("CARGO_HOME", str(home))
    monkeypatch.setenv("NUMERIC_SOURCE_SHA", "a" * 40)
    monkeypatch.setenv("NUMERIC_TOOLS_ID", "toolchain-v1")
    return cwd, home, manifest_list


@pytest.mark.parametrize("location", ["cwd", "parent", "ancestor", "home"])
@pytest.mark.parametrize("filename", ["config", "config.toml"])
@pytest.mark.parametrize(
    "setting",
    [
        '[build]\nrustflags=["-Copt-level=1"]\n',
        "[profile.release]\nopt-level=1\n",
        '[build]\ntarget="x86_64-unknown-linux-gnu"\n',
    ],
)
def test_cargo_config_creation_mutation_and_removal_change_key(
    cargo_tree, location, filename, setting
) -> None:
    cwd, home, inputs = cargo_tree
    directory = {
        "cwd": cwd / ".cargo",
        "parent": cwd.parent / ".cargo",
        "ancestor": cwd.parent.parent / ".cargo",
        "home": home,
    }[location]
    directory.mkdir(exist_ok=True)
    config = directory / filename
    absent = cache.cache_key(cwd, inputs)
    config.write_text(setting)
    created = cache.cache_key(cwd, inputs)
    assert created != absent
    config.write_text(setting.replace("1", "2").replace("x86_64", "aarch64"))
    assert cache.cache_key(cwd, inputs) != created
    config.unlink()
    assert cache.cache_key(cwd, inputs) == absent


@pytest.mark.parametrize("location", ["cwd", "parent", "home"])
@pytest.mark.parametrize("filename", ["config", "config.toml"])
def test_cargo_include_refuses_key_before_restore(
    cargo_tree, location, filename
) -> None:
    cwd, home, inputs = cargo_tree
    directory = {"cwd": cwd / ".cargo", "parent": cwd.parent / ".cargo", "home": home}[
        location
    ]
    directory.mkdir(exist_ok=True)
    (directory / filename).write_text('include = ["flags.toml"]\n')
    (directory / "flags.toml").write_text('[build]\nrustflags=["-Copt-level=1"]\n')
    with pytest.raises(ValueError, match="includes require explicit cache support"):
        cache.cache_key(cwd, inputs)


def test_identical_config_relocation_changes_key(cargo_tree) -> None:
    cwd, home, inputs = cargo_tree
    directory = cwd / ".cargo"
    directory.mkdir()
    config = directory / "config"
    config.write_text('[build]\nrustflags=["--cfg=fixture"]\n')
    before = cache.cache_key(cwd, inputs)
    config.rename(home / "config")
    assert cache.cache_key(cwd, inputs) != before


def test_cargo_config_broken_symlink_refuses_key(cargo_tree) -> None:
    cwd, home, inputs = cargo_tree
    (home / "config.toml").symlink_to(home / "missing")
    with pytest.raises(FileNotFoundError):
        cache.cache_key(cwd, inputs)
