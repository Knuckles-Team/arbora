"""Release gate: the built wheel carries every data file and boots when installed.

Every unit test runs from the source tree, where a data file left out of
``[tool.setuptools.package-data]`` is still on disk. Only an installed wheel
shows the gap (arbora 0.1.0 crash-looped on a missing ``eg_exclusions.yaml``).

Two subcommands:

``contents WHEEL``
    Run from the source checkout. Fails when a non-Python file tracked under
    ``arbora/`` is absent from ``WHEEL``.

``served URL``
    Run against a served process (the installed wheel started with the loopback
    engine). Calls ``ops.doctor``, one ``find`` and ``fleet.status`` through the
    served MCP surface and fails unless every call succeeds.

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
import asyncio
import json
import os
import subprocess  # nosec B404 -- fixed argv for the release gate
import sys
import tempfile
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

#: The served calls the fresh-install boot proof makes (ARBORA-HOST-R040.4).
BOOT_CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("ask", {"op": "ops.doctor", "params": {}}),
    ("find", {"intent": "list the served operations"}),
    ("ask", {"op": "fleet.status", "params": {}}),
)

ServedCall = Callable[[str, dict[str, Any]], Awaitable[Any]]


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


def _refusal(result: Any) -> str | None:
    """The refusal code or error text of one served answer, else ``None``."""
    if getattr(result, "is_error", False):
        return "error"
    text = "".join(getattr(c, "text", "") for c in getattr(result, "content", []) or [])
    try:
        body = json.loads(text)
    except ValueError:
        return None
    if isinstance(body, dict) and body.get("ok") is False:
        error = body.get("error")
        return (
            str((error or {}).get("code") or "unknown")
            if isinstance(error, dict)
            else "unknown"
        )
    return None


async def check_served_calls(call: ServedCall) -> list[str]:
    """Make every boot call; the failures name the call and the code, never a value."""
    failures: list[str] = []
    for tool, arguments in BOOT_CALLS:
        label = arguments.get("op") or tool
        try:
            code = _refusal(await call(tool, arguments))
        except Exception as exc:  # noqa: BLE001 - every failure is a boot failure
            code = type(exc).__name__
        if code is not None:
            failures.append(f"{label}: {code}")
    return failures


async def _served(url: str) -> list[str]:
    from fastmcp import Client
    from fastmcp.client.auth import BearerAuth

    token = os.environ.get("ARBORA_BOOT_TOKEN")
    async with Client(url, auth=BearerAuth(token) if token else None) as client:

        async def call(tool: str, arguments: dict[str, Any]) -> Any:
            return await client.call_tool(tool, arguments, raise_on_error=False)

        return await check_served_calls(call)


def check_served(url: str) -> int:
    failures = asyncio.run(_served(url))
    for line in failures:
        print(f"served boot call failed: {line}", file=sys.stderr)
    if not failures:
        print(f"served boot calls ok: {len(BOOT_CALLS)}")
    return 1 if failures else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    contents = commands.add_parser("contents")
    contents.add_argument("wheel", type=Path)
    contents.add_argument("--source-root", type=Path, default=Path.cwd())
    served = commands.add_parser("served")
    served.add_argument("url")
    commands.add_parser("runtime")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    run: dict[str, Callable[[], int]] = {
        "served": lambda: check_served(args.url),
        "contents": lambda: check_contents(args.wheel, args.source_root),
        "runtime": check_runtime,
    }
    return run[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
