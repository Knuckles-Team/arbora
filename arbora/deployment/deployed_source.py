"""Manage the explicit Arbora source-promotion worktree.

A served Arbora pod can import its source from a read-only mount. This command
owns the *source-side* part of that contract: a checkout of
``refs/heads/deployed`` at a stable path. It is deliberately a one-shot operator
command. It does not call Kubernetes, restart a pod, or poll as a daemon; the
caller performs the separately reviewed rollout after a promotion.

The command operates on the canonical source repository, because that is where
the promotion ref lives. It uses Git worktree/ref operations rather than
changing the canonical checkout's working tree, so an uncommitted main checkout remains untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 -- fixed git argv, no shell
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

PROMOTION_REF = "refs/heads/deployed"
PROMOTION_BRANCH = "deployed"
#: Environment keys; a declared config file (``--config``) or flags override them.
SOURCE_REPO_ENV = "ARBORA_SOURCE_REPO"
DEPLOYED_WORKTREE_ENV = "ARBORA_DEPLOYED_WORKTREE"
STATUS_TIMEOUT = 30
MUTATION_TIMEOUT = 180


class OperatorError(RuntimeError):
    """A fail-closed operator error suitable for a concise CLI diagnostic."""


def _git(
    directory: Path,
    *arguments: str,
    timeout: int = STATUS_TIMEOUT,
    check: bool = True,
) -> str:
    """Run Git without a shell and return trimmed stdout."""

    command = ["git", "-C", str(directory), *arguments]
    try:
        result = subprocess.run(  # nosec B603 -- fixed git argv, no shell
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperatorError(f"could not run {' '.join(command)}: {exc}") from exc
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise OperatorError(
            f"{' '.join(command)} failed ({result.returncode})"
            + (f": {detail}" if detail else "")
        )
    return result.stdout.strip()


def _git_ok(directory: Path, *arguments: str) -> bool:
    """Return whether a Git predicate exits successfully."""

    command = ["git", "-C", str(directory), *arguments]
    try:
        return (
            subprocess.run(  # nosec B603 -- fixed git argv, no shell
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=STATUS_TIMEOUT,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _absolute(path: Path, *, must_exist: bool = False) -> Path:
    """Resolve a path without requiring a not-yet-created worktree."""

    path = path.expanduser()
    if not path.is_absolute():
        raise OperatorError(f"path must be absolute: {path}")
    resolved = path.resolve(strict=must_exist)
    if resolved == Path(resolved.root):
        raise OperatorError(f"refusing filesystem root as a path: {path}")
    return resolved


def _source_repo(path: Path) -> Path:
    source = _absolute(path, must_exist=True)
    if not source.is_dir():
        raise OperatorError(f"source repository is not a directory: {source}")
    if _git(source, "rev-parse", "--is-inside-work-tree") != "true":
        raise OperatorError(f"source is not a non-bare Git worktree: {source}")
    top = _absolute(Path(_git(source, "rev-parse", "--show-toplevel")), must_exist=True)
    if top != source:
        raise OperatorError(
            f"source repository must be its checkout root (found {top}, requested {source})"
        )
    return source


def _git_path(directory: Path, argument: str) -> Path:
    """Resolve a path returned by ``git rev-parse`` relative to its checkout."""

    value = Path(_git(directory, "rev-parse", argument))
    if not value.is_absolute():
        value = directory / value
    return value.resolve()


def _worktree_identity(source: Path, worktree: Path) -> dict[str, Any]:
    """Read the actual checkout identity, not just the registry's path record."""

    if not worktree.exists():
        return {"exists": False, "valid": False}
    if not worktree.is_dir():
        return {
            "exists": True,
            "valid": False,
            "reason": "deployed worktree path exists but is not a directory",
        }
    try:
        top = _git_path(worktree, "--show-toplevel")
        common_dir = _git_path(worktree, "--git-common-dir")
        branch = _git(worktree, "symbolic-ref", "--quiet", "HEAD", check=False) or None
        head = _git(worktree, "rev-parse", "HEAD")
    except OperatorError as exc:
        return {
            "exists": True,
            "valid": False,
            "reason": "deployed worktree path is not a usable Git worktree",
            "error": str(exc),
        }
    source_common_dir = _git_path(source, "--git-common-dir")
    return {
        "exists": True,
        "valid": True,
        "top": str(top),
        "common_dir": str(common_dir),
        "source_common_dir": str(source_common_dir),
        "common_dir_matches": common_dir == source_common_dir,
        "top_matches": top == worktree,
        "branch": branch,
        "HEAD": head,
    }


def _worktree_path(path: Path, source: Path) -> Path:
    # Check the literal operator-supplied path before ``resolve()`` follows a
    # symlink.  A symlink could otherwise redirect the stable mount to an
    # unrelated checkout after validation.
    literal = path.expanduser()
    if not literal.is_absolute():
        raise OperatorError(f"path must be absolute: {literal}")
    if literal.is_symlink():
        raise OperatorError(f"deployed worktree path must not be a symlink: {literal}")
    worktree = _absolute(literal)
    if worktree == source:
        raise OperatorError(
            "deployed worktree must not be the canonical source checkout"
        )
    # A nested path can make a checkout operation recursively consume or
    # overwrite the canonical tree.  Reject both ancestor directions.
    try:
        worktree.relative_to(source)
        nested = True
    except ValueError:
        nested = False
    try:
        source.relative_to(worktree)
        parent_of_source = True
    except ValueError:
        parent_of_source = False
    if nested or parent_of_source:
        raise OperatorError(
            "deployed worktree must be outside the canonical source checkout: "
            f"{worktree}"
        )
    return worktree


def _revision(source: Path, value: str) -> str:
    """Resolve an operator-supplied commit-ish to an immutable commit SHA."""

    if not value or value.startswith("-"):
        raise OperatorError(f"invalid commit argument: {value!r}")
    try:
        return _git(source, "rev-parse", "--verify", f"{value}^{{commit}}")
    except OperatorError as exc:
        raise OperatorError(
            f"commit is not a valid commit in source repo: {value}"
        ) from exc


def _main_tip(source: Path) -> str:
    return _revision(source, "refs/heads/main")


def _deployed_tip(source: Path) -> str | None:
    value = _git(source, "rev-parse", "--verify", "--quiet", PROMOTION_REF, check=False)
    return value or None


def _worktree_records(source: Path) -> list[dict[str, str]]:
    """Parse ``git worktree list --porcelain`` without assuming table columns."""

    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in _git(source, "worktree", "list", "--porcelain").splitlines():
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, separator, value = line.partition(" ")
        if separator:
            current[key] = value
        else:
            current[key] = ""
    if current:
        records.append(current)
    return records


def _worktree_for(
    source: Path,
    worktree: Path,
    *,
    records: Iterable[dict[str, str]] | None = None,
) -> dict[str, str] | None:
    for record in records if records is not None else _worktree_records(source):
        if Path(record.get("worktree", "")).resolve() == worktree:
            return record
    return None


def _clean(worktree: Path) -> bool:
    return not bool(_git(worktree, "status", "--porcelain", "--untracked-files=all"))


def inspect(source: Path, worktree: Path) -> dict[str, Any]:
    """Return a machine-readable source-promotion health snapshot."""

    main = _main_tip(source)
    deployed = _deployed_tip(source)
    records = _worktree_records(source)
    record = _worktree_for(source, worktree, records=records)
    deployed_record = next(
        (
            candidate
            for candidate in records
            if candidate.get("branch") == PROMOTION_REF
        ),
        None,
    )
    identity = _worktree_identity(source, worktree)
    source_common_dir = _git_path(source, "--git-common-dir")

    result: dict[str, Any] = {
        "source_repo": str(source),
        "worktree": str(worktree),
        "promotion_ref": PROMOTION_REF,
        "main": main,
        "deployed": deployed,
        "source_common_dir": str(source_common_dir),
        "worktree_exists": bool(identity.get("exists")),
        "worktree_git_valid": bool(identity.get("valid")),
        "worktree_identity_valid": bool(
            identity.get("valid")
            and identity.get("common_dir_matches")
            and identity.get("top_matches")
        ),
        "worktree_common_dir": identity.get("common_dir"),
        "worktree_top": identity.get("top"),
        "worktree_branch": identity.get("branch")
        or (record.get("branch") if record else None),
        "worktree_head": identity.get("HEAD")
        or (record.get("HEAD") if record else None),
        "worktree_clean": None,
        "unpromoted_commits": None,
        "decoupled": False,
    }

    if deployed is None:
        result.update(
            state="undecoupled",
            reason=(
                f"{PROMOTION_REF} does not exist; create it explicitly with "
                "`init --initial-sha <gated-main-sha>` before switching the /au mount"
            ),
        )
        return result

    if not _git_ok(source, "merge-base", "--is-ancestor", deployed, main):
        result.update(
            state="diverged",
            reason=f"{PROMOTION_REF} is not an ancestor of refs/heads/main",
        )
        return result

    result["unpromoted_commits"] = int(
        _git(source, "rev-list", "--count", f"{deployed}..{main}") or "0"
    )

    if not identity.get("exists"):
        result.update(
            state="worktree_missing",
            reason=f"registered worktree directory is missing at {worktree}",
        )
        if deployed_record:
            result["registered_deployed_worktree"] = deployed_record.get("worktree")
        return result

    if identity.get("exists") and (
        not identity.get("valid")
        or not identity.get("common_dir_matches")
        or not identity.get("top_matches")
    ):
        reason = identity.get("reason")
        if not reason and not identity.get("common_dir_matches"):
            reason = (
                "deployed worktree uses a different git-common-dir; refusing an "
                "unrelated clone"
            )
        if not reason and not identity.get("top_matches"):
            reason = "deployed worktree Git top-level does not match the stable path"
        result.update(state="worktree_mismatch", reason=reason)
        return result

    if record is None:
        result.update(
            state="worktree_missing",
            reason=f"no registered worktree exists at {worktree}",
        )
        if deployed_record:
            result["registered_deployed_worktree"] = deployed_record.get("worktree")
        return result

    result["worktree_clean"] = _clean(worktree)
    if record.get("branch") != PROMOTION_REF or identity.get("branch") != PROMOTION_REF:
        result.update(
            state="worktree_mismatch",
            reason=f"worktree at {worktree} is not checked out at {PROMOTION_REF}",
        )
        return result
    if record.get("HEAD") != deployed or identity.get("HEAD") != deployed:
        result.update(
            state="worktree_mismatch",
            reason=(
                f"worktree HEAD {identity.get('HEAD') or record.get('HEAD')} does not match "
                f"{PROMOTION_REF} {deployed}"
            ),
        )
        return result

    result.update(state="ready", decoupled=True)
    return result


def _require_ready(source: Path, worktree: Path) -> dict[str, Any]:
    snapshot = inspect(source, worktree)
    if snapshot["state"] != "ready":
        raise OperatorError(
            f"source promotion is not ready ({snapshot['state']}): {snapshot['reason']}"
        )
    if not snapshot["worktree_clean"]:
        raise OperatorError(
            f"deployed worktree is dirty: {worktree}; preserve or remove local changes first"
        )
    return snapshot


def _require_main_ancestor(source: Path, target: str, *, label: str) -> None:
    main = _main_tip(source)
    if not _git_ok(source, "merge-base", "--is-ancestor", target, main):
        raise OperatorError(
            f"{label} {target} is not an ancestor of refs/heads/main ({main}); "
            "only merged main history may be promoted"
        )


def _ensure_parent(worktree: Path) -> None:
    try:
        worktree.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OperatorError(
            f"cannot create deployed worktree parent {worktree.parent}: {exc}"
        ) from exc


def _cleanup_failed_initialization(
    source: Path,
    worktree: Path,
    target: str,
    *,
    owns_ref: bool,
    owns_worktree: bool,
) -> list[str]:
    """Remove only artifacts proven to be owned by a failed ``init`` attempt."""

    errors: list[str] = []
    record = _worktree_for(source, worktree)
    if owns_worktree and record is not None and record.get("branch") == PROMOTION_REF:
        try:
            _git(
                source,
                "worktree",
                "remove",
                "--force",
                str(worktree),
                timeout=MUTATION_TIMEOUT,
            )
        except OperatorError as exc:
            errors.append(f"worktree cleanup failed: {exc}")
    elif (
        not owns_worktree
        and record is not None
        and record.get("branch") == PROMOTION_REF
    ):
        # A failed Git invocation does not prove whether it created the
        # registration.  Never remove a registration we cannot attribute to
        # this invocation; leave it for an operator to inspect.
        errors.append("worktree registration ownership was not proven; left intact")

    if not owns_ref:
        # The CAS create below failed, which means another actor owns the ref
        # (possibly at the exact same SHA).  In particular, do not delete it
        # merely because the value equals our requested target.
        return errors

    # The ref was created by our successful compare-and-swap.  Delete it with
    # the same expected-value guard; a concurrent update or checkout makes the
    # cleanup fail closed rather than deleting another actor's ref.
    current = _deployed_tip(source)
    # A different operator may have checked out the ref between our CAS and
    # the failed ``worktree add``.  Re-read *all* registrations after any
    # cleanup above: checking only the requested path would let a raw
    # ``update-ref -d`` delete a branch that another checkout still owns.
    deployed_records = [
        candidate
        for candidate in _worktree_records(source)
        if candidate.get("branch") == PROMOTION_REF
    ]
    if current == target and not deployed_records:
        try:
            _git(source, "update-ref", "-d", PROMOTION_REF, target)
        except OperatorError as exc:
            errors.append(f"promotion-ref cleanup failed: {exc}")
    elif current is not None and current != target:
        errors.append(
            f"promotion ref changed during cleanup ({current}); left untouched"
        )
    elif current == target:
        errors.append(
            "promotion-ref cleanup found a registered deployed worktree; left ref intact"
        )
    return errors


def initialize(source: Path, worktree: Path, initial: str) -> dict[str, Any]:
    """Create the deployed ref and checkout in one explicit operation."""

    if _deployed_tip(source) is not None:
        raise OperatorError(
            f"{PROMOTION_REF} already exists; use `ensure` and `promote`, never reinitialize it"
        )
    if _worktree_for(source, worktree) is not None:
        raise OperatorError(f"a Git worktree is already registered at {worktree}")
    if worktree.exists() and any(worktree.iterdir()):
        raise OperatorError(f"refusing non-empty deployed worktree path: {worktree}")
    target = _revision(source, initial)
    _require_main_ancestor(source, target, label="initial commit")
    owns_ref = False
    owns_worktree = False
    try:
        # ``update-ref <new> <old-empty>`` is the ownership boundary.  Git's
        # ref lock makes this a real CAS: if another actor creates deployed
        # between the preflight and this operation—even at the same SHA—we
        # fail without acquiring ownership and cleanup cannot delete it.
        _git(
            source,
            "update-ref",
            PROMOTION_REF,
            target,
            "",
            timeout=MUTATION_TIMEOUT,
        )
        owns_ref = True
        _ensure_parent(worktree)
        _git(
            source,
            "worktree",
            "add",
            str(worktree),
            PROMOTION_BRANCH,
            timeout=MUTATION_TIMEOUT,
        )
        owns_worktree = True
        snapshot = inspect(source, worktree)
        if snapshot["state"] != "ready":
            raise OperatorError(
                f"Git created an unexpected deployed state ({snapshot['state']}): "
                f"{snapshot.get('reason', 'inspect failed')}"
            )
        return snapshot
    except Exception as exc:
        cleanup_errors = _cleanup_failed_initialization(
            source,
            worktree,
            target,
            owns_ref=owns_ref,
            owns_worktree=owns_worktree,
        )
        detail = f"deployed initialization failed: {exc}"
        if cleanup_errors:
            detail += "; cleanup incomplete: " + " | ".join(cleanup_errors)
        raise OperatorError(detail) from exc


def ensure(source: Path, worktree: Path) -> dict[str, Any]:
    """Materialize an existing deployed ref at the required stable path."""

    snapshot = inspect(source, worktree)
    if snapshot["state"] == "ready":
        return snapshot
    registered = _worktree_for(source, worktree)
    if (
        registered is not None
        and registered.get("branch") == PROMOTION_REF
        and not worktree.exists()
    ):
        # Git retains a worktree admin record when the checkout directory is
        # lost (and marks it ``prunable``).  Remove only this exact stale
        # registration through Git, then recreate it below.  Do not run a
        # repository-wide ``worktree prune``: this repo has unrelated lanes.
        _git(
            source,
            "worktree",
            "remove",
            "--force",
            str(worktree),
            timeout=MUTATION_TIMEOUT,
        )
        snapshot = inspect(source, worktree)
        registered = _worktree_for(source, worktree)
    deployed = snapshot["deployed"]
    if deployed is None:
        raise OperatorError(
            "cannot ensure a worktree before the promotion ref exists; run "
            "`init --initial-sha <gated-main-sha>` once"
        )
    if snapshot["state"] == "diverged":
        raise OperatorError(snapshot["reason"])
    if snapshot["state"] == "worktree_mismatch":
        raise OperatorError(snapshot["reason"])
    if snapshot.get("registered_deployed_worktree"):
        raise OperatorError(
            f"{PROMOTION_REF} is checked out elsewhere at "
            f"{snapshot['registered_deployed_worktree']}; refusing a second checkout"
        )
    if registered is not None:
        raise OperatorError(
            f"path {worktree} is registered as a different worktree; inspect before repair"
        )
    if worktree.exists() and any(worktree.iterdir()):
        raise OperatorError(f"refusing non-empty deployed worktree path: {worktree}")
    _ensure_parent(worktree)
    _git(
        source,
        "worktree",
        "add",
        str(worktree),
        PROMOTION_BRANCH,
        timeout=MUTATION_TIMEOUT,
    )
    snapshot = inspect(source, worktree)
    if snapshot["state"] != "ready":
        raise OperatorError(
            f"Git created an unexpected deployed state ({snapshot['state']}): "
            f"{snapshot.get('reason', 'inspect failed')}"
        )
    return snapshot


def promote(source: Path, worktree: Path, target_arg: str) -> dict[str, Any]:
    """Fast-forward deployed to a gated commit already reachable from main."""

    snapshot = _require_ready(source, worktree)
    target = _revision(source, target_arg)
    _require_main_ancestor(source, target, label="promotion commit")
    deployed = str(snapshot["deployed"])
    if not _git_ok(source, "merge-base", "--is-ancestor", deployed, target):
        raise OperatorError(
            f"promotion commit {target} would rewind or diverge {PROMOTION_REF} {deployed}; "
            "use rollback for an intentional rewind"
        )
    _git(
        worktree,
        "merge",
        "--ff-only",
        "--no-edit",
        target,
        timeout=MUTATION_TIMEOUT,
    )
    result = inspect(source, worktree)
    result["action"] = "promote"
    result["promoted_commit"] = target
    result["restart_required"] = True
    return result


def rollback(
    source: Path,
    worktree: Path,
    target_arg: str,
    *,
    confirmed: bool,
) -> dict[str, Any]:
    """Move deployed back along its own history, with an explicit confirmation."""

    snapshot = _require_ready(source, worktree)
    target = _revision(source, target_arg)
    deployed = str(snapshot["deployed"])
    if not _git_ok(source, "merge-base", "--is-ancestor", target, deployed):
        raise OperatorError(
            f"rollback target {target} is not an ancestor of deployed {deployed}; "
            "rollback may only select a known prior deployed commit"
        )
    if not confirmed:
        raise OperatorError(
            "rollback changes only the deployed worktree but rewinds its branch; "
            "re-run with --confirm-rollback after reviewing status"
        )
    _git(
        worktree,
        "reset",
        "--hard",
        target,
        timeout=MUTATION_TIMEOUT,
    )
    result = inspect(source, worktree)
    result["action"] = "rollback"
    result["rolled_back_to"] = target
    result["restart_required"] = True
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Maintain Arbora's explicit refs/heads/deployed source worktree. "
            "No Kubernetes mutation is performed."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="declared file with deployed_source.source_repo and .worktree (YAML or JSON)",
    )
    parser.add_argument(
        "--source-repo",
        type=Path,
        help=f"canonical source checkout (else config, else ${SOURCE_REPO_ENV})",
    )
    parser.add_argument(
        "--worktree",
        type=Path,
        help=f"stable deployed worktree (else config, else ${DEPLOYED_WORKTREE_ENV})",
    )
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser(
        "status", help="report ref/worktree state without mutating anything"
    )
    commands.add_parser(
        "ensure",
        help="checkout an existing deployed ref at the stable path; never creates the ref",
    )
    init = commands.add_parser("init", help="one-time explicit ref/worktree creation")
    init.add_argument(
        "--initial-sha", required=True, help="merged main commit for initial deployment"
    )
    promote_parser = commands.add_parser(
        "promote", help="fast-forward deployed to a gated main commit"
    )
    promote_parser.add_argument(
        "--target-sha", required=True, help="merged main commit to promote"
    )
    rollback_parser = commands.add_parser(
        "rollback", help="rewind deployed to a known prior commit"
    )
    rollback_parser.add_argument(
        "--target-sha", required=True, help="known prior deployed commit"
    )
    rollback_parser.add_argument(
        "--confirm-rollback",
        action="store_true",
        help="confirm the intentional reset of the deployed worktree",
    )
    return parser


def _locations(args: argparse.Namespace) -> tuple[Path, Path]:
    """Flag, then declared file, then environment; there is no built-in default."""
    declared: dict[str, Any] = {}
    if args.config is not None:
        import yaml

        raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
        declared = dict(raw.get("deployed_source") or {})
    source = (
        args.source_repo
        or declared.get("source_repo")
        or os.environ.get(SOURCE_REPO_ENV)
    )
    worktree = (
        args.worktree
        or declared.get("worktree")
        or os.environ.get(DEPLOYED_WORKTREE_ENV)
    )
    if not source or not worktree:
        raise OperatorError(
            "source repository and worktree are required "
            f"(--source-repo/--worktree, --config, or ${SOURCE_REPO_ENV}/${DEPLOYED_WORKTREE_ENV})"
        )
    return Path(source), Path(worktree)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        source_arg, worktree_arg = _locations(args)
        source = _source_repo(source_arg)
        worktree = _worktree_path(worktree_arg, source)
        if args.action == "status":
            result = inspect(source, worktree)
        elif args.action == "ensure":
            result = ensure(source, worktree)
        elif args.action == "init":
            result = initialize(source, worktree, args.initial_sha)
        elif args.action == "promote":
            result = promote(source, worktree, args.target_sha)
        elif args.action == "rollback":
            result = rollback(
                source,
                worktree,
                args.target_sha,
                confirmed=args.confirm_rollback,
            )
        else:  # pragma: no cover - argparse enforces the subcommands
            raise OperatorError(f"unknown action: {args.action}")
    except (OperatorError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the CLI tests
    raise SystemExit(main())
