"""The local no-auth sandbox command (ARBORA-DEPLOY-R019, ARBORA-IDENTITY-R004.10).

``arbora-sandbox up`` starts the engine in explicit no-auth mode on loopback with
a fresh store, mints the declared sandbox principals and holds until stopped.
``arbora-sandbox down`` stops it and deletes the store, which ends every
principal. The principals are admitted by the engine from the ``sandbox`` field
of the no-auth envelope; nothing here writes a credential.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess  # nosec B404 - fixed argv, no shell
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from arbora.core.environment import inherited_environment
from arbora.identity.sandbox_principal import (
    ROLES,
    SANDBOX_TENANTS,
    SandboxPrincipalRefusal,
    mint_sandbox_principals,
)

LOOPBACK = "127.0.0.1"
STATE_FILE = "sandbox.json"
#: The explicit no-auth mode of the serving process and of the web UI co-service.
NO_AUTH_MODE = "none"
#: The web UI co-service's own listener; never the serving process's port.
DEFAULT_WEBUI_PORT = 8080


def _root(override: str | None) -> Path:
    if override:
        return Path(override)
    import platformdirs

    return (
        Path(platformdirs.user_runtime_dir("arbora", ensure_exists=False)) / "sandbox"
    )


#: Starts one process and returns its PID. Tests inject a recorder.
Launcher = Callable[[Sequence[str], Mapping[str, str]], int]
#: Stops one process by PID. Tests inject a recorder.
Terminator = Callable[[int], None]


def _launch(command: Sequence[str], environment: Mapping[str, str]) -> int:
    """Start ``command`` detached from this terminal, on loopback, and return its PID."""
    process = subprocess.Popen(  # noqa: S603  # nosec B603 - fixed argv, no shell
        list(command),
        env={**inherited_environment(), **environment},
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return process.pid


def _terminate(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)


def serving_command(serving_port: int) -> list[str]:
    """The serving process, bound to loopback. The web UI is its co-service."""
    return [
        sys.executable,
        "-c",
        "from arbora.console import main; main()",
        "--transport",
        "streamable-http",
        "--host",
        LOOPBACK,
        "--port",
        str(serving_port),
    ]


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="arbora-sandbox")
    parser.add_argument("--root", help="the sandbox state directory")
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("up", help="start the sandbox and hold until stopped")
    up.add_argument("--principal", choices=ROLES, default="operator")
    up.add_argument(
        "--second-tenant-principal",
        choices=ROLES,
        help=f"also mint a principal in {SANDBOX_TENANTS[1]} with this role",
    )
    up.add_argument("--port", type=int, default=0, help="engine loopback port")
    up.add_argument(
        "--serving-port",
        type=int,
        default=0,
        help="serving process loopback port (a free port by default)",
    )
    up.add_argument(
        "--webui-port",
        type=int,
        default=DEFAULT_WEBUI_PORT,
        help="web UI co-service loopback port",
    )
    commands.add_parser("down", help="stop the sandbox and delete its store")
    return parser.parse_args(argv)


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        return int(probe.getsockname()[1])


def render_sandbox(
    *,
    engine_port: int,
    webui_port: int = DEFAULT_WEBUI_PORT,
    bind_host: str = LOOPBACK,
) -> dict[str, Any]:
    """The sandbox environment and listeners, rendered without starting anything.

    ARBORA-DEPLOY-R019, R019.1: the serving process and the web UI co-service
    both run in the explicit no-auth mode, and every listener is loopback. A
    non-loopback bind refuses before anything is rendered.
    """
    from arbora.identity.sandbox_principal import _is_loopback

    if not _is_loopback(bind_host):
        raise SandboxPrincipalRefusal(
            "SANDBOX_PRINCIPAL_NOT_LOOPBACK",
            "the sandbox listener must be a literal loopback address",
        )
    return {
        "environment": {
            "ARBORA_AUTH_MODE": NO_AUTH_MODE,
            "WEBUI_AUTH_MODE": NO_AUTH_MODE,
            "ENABLE_WEB_UI": "true",
            "ARBORA_WEBUI_PORT": str(webui_port),
            "GRAPH_SERVICE_ENDPOINTS": f"tcp://{bind_host}:{engine_port}",
        },
        "listeners": {
            "engine": f"{bind_host}:{engine_port}",
            "webui": f"{bind_host}:{webui_port}",
        },
        "co_services": ["webui"],
    }


def resolve_engine(pinned: str | None = None, installed: str | None = None) -> str:
    """The engine version the sandbox starts: the one the package pins, or a refusal.

    ARBORA-DEPLOY-R019.2: the pin is read from the installed package metadata
    (the exact ``medulla==X`` requirement). An available engine that differs
    refuses ``SANDBOX_ENGINE_VERSION_MISMATCH`` naming both versions; a
    different engine is never started silently.
    """
    from importlib import metadata

    if pinned is None:
        requirement = next(
            (
                r
                for r in metadata.requires("arbora") or ()
                if r.split(";")[0].replace(" ", "").startswith("medulla==")
            ),
            "",
        )
        pinned = requirement.split(";")[0].replace(" ", "").removeprefix("medulla==")
    if installed is None:
        installed = metadata.version("medulla")
    if not pinned or installed != pinned:
        raise SandboxPrincipalRefusal(
            "SANDBOX_ENGINE_VERSION_MISMATCH",
            f"the engine is {installed}, the lock pins {pinned or 'none'}",
        )
    return pinned


def up(
    args: argparse.Namespace,
    *,
    hold: bool = True,
    launcher: Launcher = _launch,
) -> dict[str, Any]:
    """Start the engine, the serving process and its web UI co-service.

    ARBORA-DEPLOY-R019: the serving process starts in the rendered no-auth
    environment on loopback with the web UI co-service enabled; its PID is
    recorded so ``down`` stops exactly that process. The summary names no secret.
    """
    from arbora.mcp_server.composition import LocalEngineConfig, ensure_local_engine

    resolve_engine()
    root = _root(args.root)
    state = root / STATE_FILE
    if state.exists():
        raise SandboxPrincipalRefusal(
            "SANDBOX_PRINCIPAL_DUPLICATE", "a sandbox is already up; run down first"
        )
    pairs = [(args.principal, SANDBOX_TENANTS[0])]
    if args.second_tenant_principal:
        pairs.append((args.second_tenant_principal, SANDBOX_TENANTS[1]))
    principals = mint_sandbox_principals(pairs, profile="sandbox", bind_host=LOOPBACK)
    root.mkdir(parents=True, exist_ok=True)
    store = Path(tempfile.mkdtemp(prefix="store-", dir=root))
    port = args.port or _free_port()
    rendered = render_sandbox(
        engine_port=port, webui_port=getattr(args, "webui_port", DEFAULT_WEBUI_PORT)
    )
    config = LocalEngineConfig(
        port=port, persist_dir=store, extra_args=("--auth-mode", "none")
    )
    endpoint = ensure_local_engine(config)
    serving_port = getattr(args, "serving_port", 0) or _free_port()
    serving_pid = launcher(serving_command(serving_port), rendered["environment"])
    state.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "store": str(store),
                "address": endpoint.address,
                "processes": [serving_pid],
            }
        )
    )
    summary = {
        "address": endpoint.address,
        "serving": f"{LOOPBACK}:{serving_port}",
        "webui": rendered["listeners"]["webui"],
        "environment": rendered["environment"],
        "principals": [
            {"principal": p.principal, "tenant": p.tenant, "role": p.role}
            for p in principals
        ],
    }
    print(json.dumps(summary), flush=True)
    if hold:
        _hold()
        down(args)
    return summary


def _hold() -> None:
    stopped = False

    def stop(*_: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopped:
        time.sleep(0.5)


def down(args: argparse.Namespace, *, terminate: Terminator = _terminate) -> bool:
    """Stop the holding process, the processes ``up`` launched, and the engine; delete the store."""
    root = _root(args.root)
    state = root / STATE_FILE
    if not state.exists():
        return False
    record = json.loads(state.read_text())
    if record["pid"] != os.getpid():
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(record["pid"]), signal.SIGTERM)
            return True
    from arbora.mcp_server.composition import EngineEndpoint, stand_down_engine

    for pid in record.get("processes", []):
        terminate(int(pid))
    stand_down_engine(EngineEndpoint("tcp", record["address"]))
    shutil.rmtree(record["store"], ignore_errors=True)
    state.unlink(missing_ok=True)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        if args.command == "up":
            up(args)
        else:
            down(args)
    except SandboxPrincipalRefusal as refusal:
        print(str(refusal), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
