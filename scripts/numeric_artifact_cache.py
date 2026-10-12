"""Exact-input cache for the pinned EG numeric extension; never caches acceptance."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.machinery
import json
import os
import platform
import shutil
import sys
import sysconfig
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def input_digests(source: Path, manifest_list: Path) -> list[str]:
    """Hash the checked-in inputs and effective local tool configuration."""
    root = Path(__file__).resolve().parents[1]
    inputs = [
        root / name
        for name in ("pyproject.toml", "uv.lock", ".github/workflows/release.yml")
    ]
    inputs.append(Path(__file__).resolve())
    tracked = manifest_list.read_text().splitlines()
    inputs.extend(
        source / name
        for name in tracked
        if Path(name).name
        in {
            "Cargo.toml",
            "Cargo.lock",
            "pyproject.toml",
            "uv.lock",
            "config.toml",
            "rust-toolchain.toml",
        }
    )
    cargo_home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
    inputs.extend(
        path
        for path in (cargo_home / "config", cargo_home / "config.toml")
        if path.is_file()
    )
    pyo3_config = os.environ.get("PYO3_CONFIG_FILE")
    if pyo3_config:
        inputs.append(Path(pyo3_config))
    return [digest(path) for path in inputs]


def cache_key(source: Path, manifest_list: Path) -> str:
    """Bind the source, recipe, toolchain, interpreter, platform and build flags."""
    identity = {
        "source": os.environ["NUMERIC_SOURCE_SHA"],
        "inputs": input_digests(source, manifest_list),
        "recipe": "build_numeric_kernel.py:release:features=python",
        "tools": os.environ["NUMERIC_TOOLS_ID"],
        "python": [
            sys.implementation.name,
            sys.version,
            sysconfig.get_config_var("SOABI"),
            sysconfig.get_platform(),
        ],
        "platform": [
            platform.system(),
            platform.machine(),
            platform.release(),
            platform.libc_ver(),
            platform.freedesktop_os_release(),
        ],
        "cpu": sorted(
            {
                line
                for line in Path("/proc/cpuinfo").read_text().splitlines()
                if line.startswith(("model name", "flags", "Features"))
            }
        ),
        "environment": {
            key: value
            for key, value in sorted(os.environ.items())
            if key.startswith(
                (
                    "CARGO_",
                    "RUST",
                    "PYO3_",
                    "MATURIN_",
                    "CFLAGS",
                    "CXXFLAGS",
                    "LDFLAGS",
                    "CC",
                    "CXX",
                )
            )
            or key in {"RUNNER_OS", "RUNNER_ARCH", "ImageOS", "ImageVersion"}
        },
    }
    return (
        "numeric-v1-"
        + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    )


def artifact(directory: Path, name: str) -> Path:
    """Only a regular extension in the expected directory may be loaded/copied."""
    if name not in {
        "numeric" + suffix for suffix in importlib.machinery.EXTENSION_SUFFIXES
    }:
        raise ValueError("unexpected numeric artifact name")
    path = directory / name
    if directory.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError("numeric artifact must be a regular file")
    if path.resolve().parent != directory.resolve():
        raise ValueError("numeric artifact escaped its directory")
    return path


def smoke(source: Path, path: Path) -> None:
    """Import the source-overlay extension and perform a real numeric operation."""
    sys.path.insert(0, str(source))
    kernel = importlib.import_module("epistemic_graph.numeric")
    if kernel.__file__ is None or Path(kernel.__file__).resolve() != path.resolve():
        raise ValueError("numeric import resolved outside the intended artifact")
    if kernel.__kernel__ != "eg-numeric" or kernel.sum([1.0, 2.0, 3.0]) != 6.0:
        raise ValueError("numeric kernel marker or calculation failed")


def extensions(package: Path) -> list[Path]:
    return sorted(
        path for path in package.glob("numeric*") if path.suffix in {".so", ".pyd"}
    )


def restore(source: Path, cache: Path, key: str) -> bool:
    """A miss or any invalid candidate requests a rebuild, never acceptance."""
    try:
        receipt_path = cache / "receipt.json"
        if cache.is_symlink() or receipt_path.is_symlink():
            raise ValueError("cache receipt must be a regular file")
        receipt = json.loads(receipt_path.read_text())
        if not isinstance(receipt, dict) or receipt.get("key") != key:
            raise ValueError("numeric cache identity mismatch")
        candidate = artifact(cache, receipt["artifact"])
        if digest(candidate) != receipt["sha256"]:
            raise ValueError("numeric cache digest mismatch")
        package = source / "epistemic_graph"
        for stale in extensions(package):
            stale.unlink()
        destination = package / candidate.name
        shutil.copyfile(candidate, destination)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        print(f"numeric cache miss or rejected candidate: {type(error).__name__}")
        return False
    print("numeric cache candidate: receipt and digest passed; smoke required")
    return True


def clear(source: Path, cache: Path) -> None:
    """Discard a rejected candidate before the workflow runs the native build."""
    if cache.is_symlink() or cache.is_file():
        cache.unlink()
    elif cache.exists():
        shutil.rmtree(cache)
    package = source / "epistemic_graph"
    for stale in extensions(package):
        stale.unlink()


def built_artifact(source: Path) -> Path:
    package = source / "epistemic_graph"
    built = extensions(package)
    if len(built) != 1:
        raise ValueError("build must produce exactly one numeric extension")
    return artifact(package, built[0].name)


def record(source: Path, cache: Path, key: str) -> None:
    destination = built_artifact(source)
    # A build or verification failure cannot write a receipt or reach cache/save.
    cache.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(destination, cache / destination.name)
    (cache / "receipt.json").write_text(
        json.dumps(
            {"key": key, "artifact": destination.name, "sha256": digest(destination)}
        )
    )
    print("numeric cache rebuilt: locked-interpreter smoke passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("key", "restore", "clear", "record", "probe"))
    parser.add_argument("source", type=Path)
    parser.add_argument("path", type=Path, nargs="?")
    parser.add_argument("--key")
    args = parser.parse_args()
    if args.mode != "probe" and args.path is None:
        parser.error("this mode requires an artifact or manifest-list path")
    if args.mode in {"restore", "record"} and not args.key:
        parser.error("this mode requires --key")
    if args.mode == "key":
        print(f"key={cache_key(args.source.resolve(), args.path)}")
    elif args.mode == "probe":
        smoke(args.source.resolve(), built_artifact(args.source.resolve()))
    elif args.mode == "restore":
        raise SystemExit(
            0 if restore(args.source.resolve(), args.path, args.key) else 1
        )
    elif args.mode == "clear":
        clear(args.source.resolve(), args.path)
    else:
        record(args.source.resolve(), args.path, args.key)


if __name__ == "__main__":
    main()
