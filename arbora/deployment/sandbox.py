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
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from arbora.identity.sandbox_principal import (
    ROLES,
    SANDBOX_TENANTS,
    SandboxPrincipalRefusal,
    mint_sandbox_principals,
)

LOOPBACK = "127.0.0.1"
STATE_FILE = "sandbox.json"


def _root(override: str | None) -> Path:
    if override:
        return Path(override)
    import platformdirs

    return (
        Path(platformdirs.user_runtime_dir("arbora", ensure_exists=False)) / "sandbox"
    )


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
    commands.add_parser("down", help="stop the sandbox and delete its store")
    return parser.parse_args(argv)


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        return int(probe.getsockname()[1])


def up(args: argparse.Namespace, *, hold: bool = True) -> dict[str, Any]:
    """Start the engine and mint principals; the summary names no secret."""
    from arbora.mcp_server.composition import LocalEngineConfig, ensure_local_engine

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
    config = LocalEngineConfig(
        port=port, persist_dir=store, extra_args=("--auth-mode", "none")
    )
    endpoint = ensure_local_engine(config)
    state.write_text(
        json.dumps(
            {"pid": os.getpid(), "store": str(store), "address": endpoint.address}
        )
    )
    summary = {
        "address": endpoint.address,
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


def down(args: argparse.Namespace) -> bool:
    """Stop the holding process, stand the engine down and delete the store."""
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
