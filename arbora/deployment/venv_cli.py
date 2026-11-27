"""The venv sync command line: the argument parser, the subcommands and the dispatch (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from arbora.deployment.venv_backup import (
    LockBackupStore,
)
from arbora.deployment.venv_plan import (
    VenvSyncError,
    Workspace,
    WorkspaceNotFoundError,
)
from arbora.deployment.venv_probes import (
    SANCTIONED_SYNC_FLAGS,
    LeaseActivityProbe,
    detect_activity,
    plan_sync,
)
from arbora.deployment.venv_sync import (
    SyncContext,
    acquire_lease,
    detect_drift,
    evaluate_plan,
    member_install_states,
    prune,
    release_lease,
    rollback,
    session_start_hint,
    sync,
    upgrade,
    verify,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def emit(payload: dict[str, Any], *, as_json: bool) -> None:
    """Render a result payload as JSON or as an indented human report."""
    if as_json:
        json.dump(payload, sys.stdout, indent=2, sort_keys=True, default=str)
        sys.stdout.write("\n")
        return
    _print_human(payload)


def _print_human(payload: dict[str, Any], indent: int = 0) -> None:
    pad = "  " * indent
    for key, value in payload.items():
        if isinstance(value, dict):
            sys.stdout.write(f"{pad}{key}:\n")
            _print_human(value, indent + 1)
        elif isinstance(value, list):
            sys.stdout.write(f"{pad}{key}:\n")
            for item in value:
                if isinstance(item, dict):
                    _print_human(item, indent + 1)
                    sys.stdout.write("\n")
                else:
                    sys.stdout.write(f"{pad}  - {item}\n")
        else:
            sys.stdout.write(f"{pad}{key}: {value}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arbora-venv",
        description=(
            "The sanctioned entry point for the shared uv-workspace virtualenv. "
            "Every sync it runs uses `uv sync "
            f"{' '.join(SANCTIONED_SYNC_FLAGS)}`; the destructive bare form "
            "cannot be produced by any code path here."
        ),
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="workspace root (default: discovered by walking up for [tool.uv.workspace])",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--verbose", action="store_true", help="log uv invocations to stderr"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="drift report: lock, environment, members, floors")

    sub.add_parser("plan", help="show the sanctioned sync plan (read-only)")

    sync_cmd = sub.add_parser("sync", help="make the environment match uv.lock")
    sync_cmd.add_argument("--dry-run", action="store_true")
    sync_cmd.add_argument(
        "--ignore-activity",
        action="store_true",
        help="proceed even though other lanes are using the environment",
    )
    sync_cmd.add_argument(
        "--allow-uninstalls",
        type=int,
        default=0,
        metavar="N",
        help=(
            "sanction up to N uninstalls of a LOCKED distribution or workspace "
            "member (default 0 — any such uninstall refuses). Never reaches a "
            "merely-extraneous package: this sync always runs --inexact, so "
            "uv's own plan never proposes removing one; use `prune` for that"
        ),
    )
    sync_cmd.add_argument("--reason", default="manual")

    prune_cmd = sub.add_parser(
        "prune",
        help=(
            "remove installed packages that are neither locked nor a "
            "workspace member (D-VS-8) — the class --inexact deliberately "
            "leaves behind; never runs `uv sync`"
        ),
    )
    prune_cmd.add_argument(
        "--allow-uninstalls",
        type=int,
        default=0,
        metavar="N",
        required=True,
        help="required: sanction up to N removals (0 always refuses, on purpose)",
    )
    prune_cmd.add_argument("--dry-run", action="store_true")
    prune_cmd.add_argument(
        "--ignore-activity",
        action="store_true",
        help="proceed even though other lanes are using the environment",
    )

    up = sub.add_parser(
        "upgrade", help="move dependencies forward, verify, auto-roll-back"
    )
    up.add_argument("--package", action="append", default=[], dest="packages")
    up.add_argument("--all", action="store_true", dest="all_packages")
    up.add_argument("--ignore-activity", action="store_true")
    up.add_argument("--reason", default="manual upgrade")

    relock = sub.add_parser(
        "relock", help="re-resolve uv.lock in place (backed up + verified)"
    )
    relock.add_argument("--ignore-activity", action="store_true")

    rb = sub.add_parser("rollback", help="restore an archived uv.lock and re-sync")
    rb.add_argument("--to", dest="backup_id", default=None)
    rb.add_argument("--ignore-activity", action="store_true")

    sub.add_parser("backups", help="list archived uv.lock revisions")
    sub.add_parser("verify", help="run the post-change health probes")
    sub.add_parser("members", help="per-member editable install state")
    sub.add_parser("activity", help="what the activity probes currently see")
    sub.add_parser(
        "session-hint",
        help=(
            "near-zero-cost drift check (D-VS-6), safe for a SessionStart hook: "
            "silent when clean, never raises, no `uv` subprocess"
        ),
    )

    lease = sub.add_parser("lease", help="declare/release a do-not-swap window")
    lease.add_argument("action", choices=["acquire", "release", "list"])
    lease.add_argument("--owner", default=f"pid-{os.getpid()}")
    lease.add_argument("--ttl", type=float, default=3600.0)
    lease.add_argument("--reason", default="in-flight work")

    # Trigger/hook verbs live in venv_autosync and are imported lazily so a
    # broken environment can still run `status`/`sync`.
    autosync = sub.add_parser("autosync", help="merge-triggered auto-flip control")
    autosync.add_argument(
        "action",
        choices=["on", "off", "status", "install", "uninstall", "drain", "trigger"],
    )
    autosync.add_argument(
        "--repo",
        action="append",
        default=[],
        help="repository to install/uninstall hooks in (default: every workspace member)",
    )
    autosync.add_argument("--event", default="manual")
    autosync.add_argument("--inline", action="store_true", help="reconcile in-process")
    autosync.add_argument("--ignore-activity", action="store_true")

    return parser


def run_cli(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        workspace = Workspace.discover(args.workspace)
    except WorkspaceNotFoundError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    try:
        return _dispatch(args, workspace)
    except VenvSyncError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1


def _cmd_status(args: argparse.Namespace, workspace: Workspace, as_json: bool) -> int:
    report = detect_drift(workspace)
    emit(report.as_dict(), as_json=as_json)
    return {"ok": 0, "warn": 0, "fail": 3}[report.status]


def _cmd_plan(args: argparse.Namespace, workspace: Workspace, as_json: bool) -> int:
    plan = plan_sync(workspace)
    ctx = SyncContext(workspace=workspace, activity=detect_activity(workspace))
    verdict = evaluate_plan(plan, ctx)
    emit(
        {
            "installs": [f"{d.name}=={d.version}" for d in plan.installs],
            "uninstalls": [f"{d.name}=={d.version}" for d in plan.uninstalls],
            "verdict": verdict.as_dict(),
        },
        as_json=as_json,
    )
    return 0 if verdict.allowed else 3


def _cmd_sync(args: argparse.Namespace, workspace: Workspace, as_json: bool) -> int:
    sync_outcome = sync(
        workspace,
        reason=args.reason,
        apply=not args.dry_run,
        ignore_activity=args.ignore_activity,
        allow_uninstalls=args.allow_uninstalls,
    )
    emit(sync_outcome.as_dict(), as_json=as_json)
    return 0 if sync_outcome.verdict.allowed else 3


def _cmd_prune(args: argparse.Namespace, workspace: Workspace, as_json: bool) -> int:
    prune_outcome = prune(
        workspace,
        allow_uninstalls=args.allow_uninstalls,
        apply=not args.dry_run,
        ignore_activity=args.ignore_activity,
    )
    emit(prune_outcome.as_dict(), as_json=as_json)
    return 0 if not prune_outcome.refused else 3


def _cmd_upgrade(args: argparse.Namespace, workspace: Workspace, as_json: bool) -> int:
    upgrade_outcome = upgrade(
        workspace,
        packages=getattr(args, "packages", []),
        all_packages=getattr(args, "all_packages", False) or args.command == "relock",
        reason=getattr(args, "reason", args.command),
        ignore_activity=args.ignore_activity,
    )
    emit(upgrade_outcome.as_dict(), as_json=as_json)
    return 0 if upgrade_outcome.ok else 3


def _cmd_rollback(args: argparse.Namespace, workspace: Workspace, as_json: bool) -> int:
    rollback_outcome = rollback(
        workspace, args.backup_id, ignore_activity=args.ignore_activity
    )
    emit(rollback_outcome.as_dict(), as_json=as_json)
    return 0 if rollback_outcome.verdict.allowed else 3


def _cmd_backups(args: argparse.Namespace, workspace: Workspace, as_json: bool) -> int:
    emit(
        {"backups": [b.as_dict() for b in LockBackupStore(workspace).list()]},
        as_json=as_json,
    )
    return 0


def _cmd_verify(args: argparse.Namespace, workspace: Workspace, as_json: bool) -> int:
    probes = verify(workspace)
    emit({"probes": [p.as_dict() for p in probes]}, as_json=as_json)
    return 0 if all(p.ok is not False for p in probes) else 3


def _cmd_members(args: argparse.Namespace, workspace: Workspace, as_json: bool) -> int:
    states = member_install_states(workspace)
    emit(
        {
            "members": [
                {
                    "name": s.member.name,
                    "installed": s.installed,
                    "editable": s.editable,
                    "source_version": s.source_version,
                    "installed_version": s.installed_version,
                    "differences": list(s.differences),
                }
                for s in states
            ],
            "stale": sum(1 for s in states if s.stale),
        },
        as_json=as_json,
    )
    return 0 if not any(s.stale for s in states) else 3


def _cmd_session_hint(
    args: argparse.Namespace, workspace: Workspace, as_json: bool
) -> int:
    # D-VS-6: deliberately never propagates a raised error and always
    # exits 0 — a SessionStart hook command that can fail a session is a
    # worse outcome than staying silent about drift for one session.
    hint = session_start_hint(workspace)
    if hint is not None:
        if as_json:
            emit({"hint": hint}, as_json=True)
        else:
            print(hint)
    return 0


def _cmd_activity(args: argparse.Namespace, workspace: Workspace, as_json: bool) -> int:
    records = detect_activity(workspace)
    emit(
        {
            "busy": bool(records),
            "records": [
                {"probe": r.probe, "id": r.identifier, "detail": r.detail}
                for r in records
            ],
        },
        as_json=as_json,
    )
    return 0


def _cmd_lease(args: argparse.Namespace, workspace: Workspace, as_json: bool) -> int:
    if args.action == "acquire":
        path = acquire_lease(workspace, args.owner, ttl=args.ttl, reason=args.reason)
        emit({"lease": str(path), "owner": args.owner}, as_json=as_json)
        return 0
    if args.action == "release":
        emit({"released": release_lease(workspace, args.owner)}, as_json=as_json)
        return 0
    lease_records = LeaseActivityProbe().busy(workspace)
    emit(
        {
            "leases": [
                {"owner": r.identifier, "detail": r.detail} for r in lease_records
            ]
        },
        as_json=as_json,
    )
    return 0


def _cmd_autosync(args: argparse.Namespace, workspace: Workspace, as_json: bool) -> int:
    from arbora.deployment import venv_autosync

    return venv_autosync.dispatch(args, workspace, as_json=as_json)


_COMMAND_HANDLERS: Mapping[
    str, Callable[[argparse.Namespace, Workspace, bool], int]
] = MappingProxyType(
    {
        "status": _cmd_status,
        "plan": _cmd_plan,
        "sync": _cmd_sync,
        "prune": _cmd_prune,
        "upgrade": _cmd_upgrade,
        "relock": _cmd_upgrade,
        "rollback": _cmd_rollback,
        "backups": _cmd_backups,
        "verify": _cmd_verify,
        "members": _cmd_members,
        "session-hint": _cmd_session_hint,
        "activity": _cmd_activity,
        "lease": _cmd_lease,
        "autosync": _cmd_autosync,
    }
)


def _dispatch(args: argparse.Namespace, workspace: Workspace) -> int:
    as_json = args.json
    handler = _COMMAND_HANDLERS.get(args.command)
    if handler is None:
        raise VenvSyncError(f"unhandled command {args.command!r}")
    return handler(args, workspace, as_json)
