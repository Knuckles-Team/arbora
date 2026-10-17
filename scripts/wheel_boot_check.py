"""Release gate: the built wheel carries every data file and boots when installed.

Every unit test runs from the source tree, where a data file left out of
``[tool.setuptools.package-data]`` is still on disk. Only an installed wheel
shows the gap (arbora 0.1.0 crash-looped on a missing ``eg_exclusions.yaml``).

Two subcommands:

``contents WHEEL``
    Run from the source checkout. Fails when a non-Python file tracked under
    ``arbora/`` is absent from ``WHEEL``.

``runtime``
    Run with the interpreter of a clean venv that installed the wheel, from a
    directory outside the source tree, as ``python -I``. Fails unless arbora
    imports from that venv, every file in the installed distribution's
    ``RECORD`` exists, the full operation registry builds (EG exclusions and
    the default policy load), and ``arbora --help`` / ``--version`` exit 0
    without a configuration.
"""

from __future__ import annotations

import argparse
import os
import subprocess  # nosec B404 -- fixed argv for the release gate
import sys
import tempfile
import zipfile
from pathlib import Path


def _tracked_data_files(root: Path) -> list[str]:
    listed = subprocess.run(  # nosec B603 B607 -- fixed git argv
        ["git", "ls-files", "-z", "--", "arbora"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    return sorted(
        path for path in listed.split("\0") if path and not path.endswith(".py")
    )


def check_contents(wheel: Path, root: Path) -> int:
    with zipfile.ZipFile(wheel) as archive:
        shipped = set(archive.namelist())
    expected = _tracked_data_files(root)
    missing = [path for path in expected if path not in shipped]
    for path in missing:
        print(f"missing from wheel: {path}", file=sys.stderr)
    if missing:
        print(
            f"{len(missing)} data file(s) under arbora/ are not package data; "
            "extend [tool.setuptools.package-data] in pyproject.toml",
            file=sys.stderr,
        )
        return 1
    print(f"wheel carries all {len(expected)} tracked data files under arbora/")
    return 0


def _assert_installed_copy() -> Path:
    import arbora

    package = Path(arbora.__file__).resolve().parent
    if (package.parent / "pyproject.toml").is_file():
        raise SystemExit(f"arbora imports from a source tree, not the wheel: {package}")
    if Path(sys.prefix).resolve() not in package.parents:
        raise SystemExit(f"arbora is not installed in this venv: {package}")
    return package


def _assert_record_complete() -> int:
    from importlib import metadata

    distribution = metadata.distribution("arbora")
    missing = [
        str(entry)
        for entry in distribution.files or ()
        if not Path(str(distribution.locate_file(entry))).is_file()
    ]
    if missing:
        raise SystemExit(f"installed files missing: {missing}")
    return len(distribution.files or ())


def _build_registry() -> int:
    from arbora.api.ops.registry_factory import get_registry
    from arbora.api.policy.defaults import default_policy_file
    from arbora.api.policy.pdp_remote import load_policy_file
    from arbora.mcp_server.operation_registry import published_operation_registry

    registry = get_registry()
    load_policy_file(str(default_policy_file()))
    published_operation_registry()
    return len(registry)


def _run_cli(*arguments: str) -> None:
    executable = Path(sys.executable).with_name("arbora")
    if not executable.exists():
        executable = executable.with_suffix(".exe")
    with tempfile.TemporaryDirectory(prefix="arbora-noconfig-") as empty:
        environment = {
            **os.environ,
            "HOME": empty,
            "XDG_CONFIG_HOME": empty,
            "ARBORA_CONFIG_DIR": str(Path(empty) / "absent"),
        }
        completed = subprocess.run(  # nosec B603 -- installed console script
            [str(executable), *arguments],
            env=environment,
            cwd=empty,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    if completed.returncode != 0:
        raise SystemExit(
            f"arbora {' '.join(arguments)} exited {completed.returncode}\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    print(f"arbora {' '.join(arguments)}: ok")


def check_runtime() -> int:
    package = _assert_installed_copy()
    files = _assert_record_complete()
    operations = _build_registry()
    _run_cli("--version")
    _run_cli("--help")
    print(f"installed arbora at {package}: {files} files, {operations} operations")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    contents = commands.add_parser("contents")
    contents.add_argument("wheel", type=Path)
    contents.add_argument("--source-root", type=Path, default=Path.cwd())
    commands.add_parser("runtime")
    args = parser.parse_args(argv)
    if args.command == "contents":
        return check_contents(args.wheel, args.source_root)
    return check_runtime()


if __name__ == "__main__":
    raise SystemExit(main())
