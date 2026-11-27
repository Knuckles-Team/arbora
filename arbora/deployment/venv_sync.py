"""Sanctioned reconciler for the shared uv-workspace virtualenv.

CONCEPT:MERISTEM-OS.deployment.workspace-venv-reconciler
CONCEPT:MERISTEM-OS.safety.destructive-sync-refusal
CONCEPT:MERISTEM-OS.host.venv-drift-detector

Why this module exists
----------------------
The ecosystem is developed against ONE shared ``.venv`` at the uv-workspace
root, with ~75 workspace members installed editable into it and ~26 git
worktrees running their tests through it.  Two properties of that arrangement
are actively dangerous and both were paid for in production time:

1. **A bare ``uv sync`` destroys the environment.**  The workspace root project
   declares zero dependencies, so its target set is empty and ``uv sync``
   prunes everything that is not in it — a measured *557* uninstalls including
   every editable member.  The only correct invocation is
   ``uv sync --locked --all-packages --inexact``.  Neither ``--all-packages``
   nor ``--inexact`` has a ``UV_*`` environment equivalent, so the safe form
   cannot be made the shell default; it has to be made the *only* form any
   sanctioned path can construct.  :class:`SyncInvocation` therefore has no
   field that can drop a mandatory flag, and :func:`_assert_sanctioned` re-checks
   the argv immediately before ``subprocess`` sees it, so a future refactor that
   assembles an argv somewhere else still cannot get a destructive one past this
   module.
2. **Drift is silent.**  The venv sat ten days behind its own lock
   (``fastmcp`` 3.4.4 / ``mcp`` 1.28.1 against a lock wanting 4.0.0b1 / 2.0.0).
   An entire test module stopped collecting because one import failed, hiding
   thirteen real defects.  Nothing checked, so nobody knew.  :func:`detect_drift`
   is the check, and it is wired into ``arbora-doctor`` so it runs
   without anyone remembering to run it.

Design constraints this module honours
--------------------------------------
* **Stdlib only.**  This is the tool you reach for when the venv is broken, so
  it must not import anything the venv provides — including the rest of
  ``meristem``.  Optional third-party helpers (``packaging``) are probed
  lazily and always have a stdlib fallback.
* **The workspace root is not a git repository.**  Discovery walks up looking
  for a ``pyproject.toml`` carrying ``[tool.uv.workspace]``; it never shells
  ``git rev-parse``, which is what made ``scripts/uv_workspace.py`` unusable
  from the root.
* **Another actor mutates the venv concurrently.**  Every mutation runs under an
  advisory exclusive lock and re-plans inside the lock, and every guardrail can
  answer ``defer`` rather than ``refuse`` so a busy environment is left alone
  instead of being fought over.
* **The lock has no version control.**  ``uv.lock`` is untracked and lives at a
  non-git root, so :class:`LockBackupStore` is the only rollback path there is;
  every mutation is wrapped in a checkpoint before it starts.
* **Abstraction first.**  Guardrails, activity probes and verify probes are
  registries of small protocols.  The default set encodes today's known hazards;
  adding a hazard means registering a probe, not editing a policy branch.

No new environment variables are introduced (configuration discipline): every
knob is a constructor/CLI argument, and the on/off state lives in one JSON
document under the XDG state directory.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import tomllib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from arbora.deployment.venv_backup import (
    LockBackupStore,
    _atomic_write_text,
    _unlink_quietly,
)
from arbora.deployment.venv_plan import (
    Member,
    SyncPlan,
    VenvSyncError,
    Workspace,
    canonical_name,
    redact_path_for_log,
)
from arbora.deployment.venv_probes import (
    VERIFY_PROBES,
    ActivityRecord,
    SdkFloorProbe,
    SyncInvocation,
    detect_activity,
    lock_check,
    plan_sync,
    run_uv,
)

from .venv_sync_types import (
    DriftFinding,
    DriftReport,
    MemberInstallState,
    ProbeResult,
    PruneCandidate,
    PruneOutcome,
    PrunePlan,
    SyncOutcome,
    UpgradeOutcome,
    _InstalledRecord,
)

logger = logging.getLogger(__name__)


__all__ = [
    "Guardrail",
    "LockBusyError",
    "SyncContext",
    "Verdict",
    "classify_change",
    "detect_drift",
    "evaluate_plan",
    "exclusive_lock",
    "main",
    "member_install_states",
    "plan_prune",
    "prune",
    "register_guardrail",
    "rollback",
    "session_start_hint",
    "sync",
    "upgrade",
    "verify",
]


class LockBusyError(VenvSyncError):
    """Another reconciler holds the exclusive writer lock."""


# ─────────────────────────────────────────────────────────────────────────────
# Guardrails
# ─────────────────────────────────────────────────────────────────────────────
ALLOW = "allow"
REFUSE = "refuse"
DEFER = "defer"


@dataclass(frozen=True)
class Verdict:
    """A guardrail decision."""

    decision: str
    guardrail: str = "none"
    reason: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "guardrail": self.guardrail,
            "reason": self.reason,
            "data": self.data,
        }


@dataclass
class SyncContext:
    """Everything a guardrail needs to judge a proposed change."""

    workspace: Workspace
    reason: str = "manual"
    #: How many uninstalls the caller has explicitly sanctioned.  Zero for every
    #: automatic path; only an operator running ``prune`` raises it.
    allow_uninstalls: int = 0
    #: Set when the caller has explicitly accepted running while others work.
    ignore_activity: bool = False
    activity: tuple[ActivityRecord, ...] = ()
    lock_check_ok: bool | None = None
    lock_check_detail: str = ""


@runtime_checkable
class Guardrail(Protocol):
    """Vetoes or defers a proposed environment change."""

    name: str
    #: ``True`` when the guardrail only needs the context (runs before planning).
    pre_plan: bool

    def evaluate(self, plan: SyncPlan | None, ctx: SyncContext) -> Verdict | None:
        """Return a non-``allow`` verdict to stop the change, or ``None``."""


class ActivityGuardrail:
    """Defer — never refuse — while another lane is using the environment.

    Deferring is the whole point: swapping packages underneath a running test
    run produces failures that look like code defects.  The intent is queued and
    drained later rather than dropped.
    """

    name = "activity"
    pre_plan = True

    def evaluate(self, plan: SyncPlan | None, ctx: SyncContext) -> Verdict | None:
        if ctx.ignore_activity or not ctx.activity:
            return None
        return Verdict(
            decision=DEFER,
            guardrail=self.name,
            reason=(
                f"{len(ctx.activity)} in-flight activity record(s) on the shared "
                "environment; deferring rather than swapping packages underneath "
                "running work"
            ),
            data={"activity": [record.detail for record in ctx.activity]},
        )


class LockConsistencyGuardrail:
    """Refuse to sync when ``uv lock --check`` fails.

    A stale lock means somebody changed a manifest.  Resolving that is an
    explicit ``upgrade``/relock decision with a backup and a verification pass,
    never a side effect of "keep the venv current".
    """

    name = "lock_consistency"
    pre_plan = True

    def evaluate(self, plan: SyncPlan | None, ctx: SyncContext) -> Verdict | None:
        if ctx.lock_check_ok is not False:
            return None
        return Verdict(
            decision=REFUSE,
            guardrail=self.name,
            reason=(
                "uv.lock is out of date with the workspace manifests; a sync "
                "would install a resolution nobody reviewed. Run "
                "`arbora-venv relock` (backed up + verified) instead."
            ),
            data={"detail": ctx.lock_check_detail},
        )


class MemberUninstallGuardrail:
    """Refuse any plan that would uninstall a workspace member.

    This is the guardrail that makes the measured catastrophe impossible: a
    bare ``uv sync`` plans 557 uninstalls including every editable member.
    """

    name = "member_uninstall"
    pre_plan = False

    def evaluate(self, plan: SyncPlan | None, ctx: SyncContext) -> Verdict | None:
        if plan is None or not plan.removals:
            return None
        members = {member.canonical for member in ctx.workspace.members()}
        offending = [d for d in plan.removals if d.canonical in members]
        if not offending:
            return None
        names = ", ".join(sorted(d.name for d in offending)[:8])
        return Verdict(
            decision=REFUSE,
            guardrail=self.name,
            reason=(
                f"plan would uninstall {len(offending)} editable workspace "
                f"member(s) ({names}"
                f"{', …' if len(offending) > 8 else ''}). This is the signature "
                "of an unsanctioned `uv sync`; the environment was not touched."
            ),
            data={"members": sorted(d.name for d in offending)},
        )


class LockedDistributionUninstallGuardrail:
    """Refuse to uninstall anything the lock still wants.

    ``--inexact`` means uv should only ever be adding or replacing.  An
    uninstall of a locked distribution means the plan was computed against a
    different target set than we think, so we stop.
    """

    name = "locked_uninstall"
    pre_plan = False

    def evaluate(self, plan: SyncPlan | None, ctx: SyncContext) -> Verdict | None:
        if plan is None or not plan.removals:
            return None
        locked = _locked_distribution_names(ctx.workspace)
        if not locked:
            return None
        offending = sorted({d.name for d in plan.removals if d.canonical in locked})
        if not offending:
            return None
        return Verdict(
            decision=REFUSE,
            guardrail=self.name,
            reason=(
                f"plan would uninstall {len(offending)} distribution(s) that "
                "uv.lock still requires; the environment was not touched."
            ),
            data={"distributions": offending[:32]},
        )


class UninstallBudgetGuardrail:
    """Refuse when a plan exceeds the caller's sanctioned uninstall budget."""

    name = "uninstall_budget"
    pre_plan = False

    def evaluate(self, plan: SyncPlan | None, ctx: SyncContext) -> Verdict | None:
        if plan is None:
            return None
        count = len(plan.removals)
        if count <= ctx.allow_uninstalls:
            return None
        return Verdict(
            decision=REFUSE,
            guardrail=self.name,
            reason=(
                f"plan would uninstall {count} package(s) but only "
                f"{ctx.allow_uninstalls} are sanctioned for this operation; "
                "re-run with an explicit --allow-uninstalls budget if this is "
                "intended."
            ),
            data={"count": count, "budget": ctx.allow_uninstalls},
        )


GUARDRAILS: list[Guardrail] = [
    ActivityGuardrail(),
    LockConsistencyGuardrail(),
    MemberUninstallGuardrail(),
    LockedDistributionUninstallGuardrail(),
    UninstallBudgetGuardrail(),
]


def register_guardrail(guardrail: Guardrail, *, replace_existing: bool = True) -> None:
    """Add (or replace) a guardrail."""

    if replace_existing:
        GUARDRAILS[:] = [g for g in GUARDRAILS if g.name != guardrail.name]
    GUARDRAILS.append(guardrail)


def evaluate_plan(plan: SyncPlan | None, ctx: SyncContext) -> Verdict:
    """Run the guardrails and return the first non-allow verdict.

    Refusals win over deferrals: a plan that would destroy the environment is
    wrong regardless of whether now is a good time to run it.
    """

    verdicts: list[Verdict] = []
    for guardrail in GUARDRAILS:
        if plan is None and not guardrail.pre_plan:
            continue
        verdict = guardrail.evaluate(plan, ctx)
        if verdict is not None and verdict.decision != ALLOW:
            verdicts.append(verdict)
    for verdict in verdicts:
        if verdict.decision == REFUSE:
            return verdict
    if verdicts:
        return verdicts[0]
    return Verdict(decision=ALLOW)


def _locked_distribution_names(workspace: Workspace) -> frozenset[str]:
    """Distribution names present in ``uv.lock``.

    Parsed with ``tomllib`` when the lock is well formed; a lock uv itself
    wrote always is.  A parse failure is reported and treated as "unknown",
    which simply makes this one guardrail inert — the member guardrail and the
    uninstall budget still apply.
    """

    try:
        document = tomllib.loads(workspace.lock.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning(
            "could not read %s for guardrail evaluation: %s", workspace.lock, exc
        )
        return frozenset()
    packages = document.get("package", [])
    if not isinstance(packages, list):
        return frozenset()
    return frozenset(
        canonical_name(pkg["name"])
        for pkg in packages
        if isinstance(pkg, dict) and isinstance(pkg.get("name"), str)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Exclusive writer lock
# ─────────────────────────────────────────────────────────────────────────────
def _acquire_writer_lock(lock: Any, path: Path, *, blocking: bool) -> None:
    from filelock import Timeout

    try:
        lock.acquire(timeout=-1 if blocking else 0)
    except Timeout:
        raise LockBusyError(
            f"another reconciler holds {path}; not competing for the shared venv"
        ) from None


@contextmanager
def exclusive_lock(workspace: Workspace, *, blocking: bool = False) -> Iterator[Path]:
    """Serialise every mutation of the shared venv/lock across processes.

    Advisory ``flock`` on a state-directory file.  Non-blocking by default so a
    merge hook that arrives while a reconcile is already running exits
    immediately instead of piling up — its intent stays queued and the running
    reconciler (or the next one) drains it.
    """

    from filelock import FileLock

    workspace.state_dir.mkdir(parents=True, exist_ok=True)
    path = workspace.state_dir / "writer.lock"
    lock = FileLock(path)
    try:
        _acquire_writer_lock(lock, path, blocking=blocking)
        path.write_text(
            f"{os.getpid()} {datetime.now(UTC).isoformat()}\n", encoding="utf-8"
        )
        yield path
    finally:
        lock.release()


# ─────────────────────────────────────────────────────────────────────────────
# Sync
# ─────────────────────────────────────────────────────────────────────────────
def sync(
    workspace: Workspace,
    *,
    reason: str = "manual",
    apply: bool = True,
    ignore_activity: bool = False,
    allow_uninstalls: int = 0,
    hold_lock: bool = True,
) -> SyncOutcome:
    """Bring the shared venv to the locked state, or explain why we did not.

    The whole safety story lives here:

    1. take the exclusive writer lock (``defer`` if somebody else holds it);
    2. gather context — activity probes and ``uv lock --check``;
    3. run the pre-plan guardrails (``defer`` on busy, ``refuse`` on stale lock);
    4. compute the plan with ``--dry-run`` **using the sanctioned flags**;
    5. run the plan guardrails (``refuse`` on any member/locked uninstall);
    6. only then apply — with the identical, re-asserted argv.
    """

    started = time.monotonic()
    try:
        if hold_lock:
            with exclusive_lock(workspace):
                return _sync_locked(
                    workspace,
                    reason=reason,
                    apply=apply,
                    ignore_activity=ignore_activity,
                    allow_uninstalls=allow_uninstalls,
                    started=started,
                )
        return _sync_locked(
            workspace,
            reason=reason,
            apply=apply,
            ignore_activity=ignore_activity,
            allow_uninstalls=allow_uninstalls,
            started=started,
        )
    except LockBusyError as exc:
        return SyncOutcome(
            verdict=Verdict(
                decision=DEFER,
                guardrail="writer_lock",
                reason=str(exc),
            ),
            plan=None,
            applied=False,
            detail=str(exc),
            duration_s=time.monotonic() - started,
        )


def _sync_locked(
    workspace: Workspace,
    *,
    reason: str,
    apply: bool,
    ignore_activity: bool,
    allow_uninstalls: int,
    started: float,
) -> SyncOutcome:
    check = lock_check(workspace)
    ctx = SyncContext(
        workspace=workspace,
        reason=reason,
        allow_uninstalls=allow_uninstalls,
        ignore_activity=ignore_activity,
        activity=detect_activity(workspace),
        lock_check_ok=check.ok,
        lock_check_detail=check.output[-2000:],
    )

    verdict = evaluate_plan(None, ctx)
    if not verdict.allowed:
        return SyncOutcome(
            verdict=verdict,
            plan=None,
            applied=False,
            detail=verdict.reason,
            duration_s=time.monotonic() - started,
        )

    plan = plan_sync(workspace)
    verdict = evaluate_plan(plan, ctx)
    if not verdict.allowed:
        return SyncOutcome(
            verdict=verdict,
            plan=plan,
            applied=False,
            detail=verdict.reason,
            duration_s=time.monotonic() - started,
        )

    if plan.is_empty:
        return SyncOutcome(
            verdict=verdict,
            plan=plan,
            applied=False,
            detail="environment already matches the lock; nothing to do",
            duration_s=time.monotonic() - started,
        )

    if not apply:
        return SyncOutcome(
            verdict=verdict,
            plan=plan,
            applied=False,
            detail=(
                f"plan approved but not applied (--dry-run): "
                f"{len(plan.installs)} install(s), {len(plan.removals)} removal(s)"
            ),
            duration_s=time.monotonic() - started,
        )

    result = run_uv(workspace, SyncInvocation().argv(workspace.uv)[1:])
    if not result.ok:
        raise VenvSyncError(
            f"sanctioned uv sync failed (rc={result.returncode}): {result.output[-2000:]}"
        )
    return SyncOutcome(
        verdict=verdict,
        plan=plan,
        applied=True,
        detail=(
            f"applied {len(plan.installs)} install(s), "
            f"{len(plan.removals)} removal(s), {len(plan.replacements)} replacement(s)"
        ),
        duration_s=time.monotonic() - started,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Prune (D-VS-8)
#
# ``--inexact`` — permanently forced into SANCTIONED_SYNC_FLAGS, unconditionally
# — means uv's own dry-run plan NEVER reports a genuinely extraneous package as
# a removal. Verified live 2026-07-31 against a real throwaway uv workspace: an
# installed distribution absent from the lock survived `sync(workspace,
# allow_uninstalls=1)` completely untouched — `plan_sync()` returned
# `installs=[], uninstalls=[]` even though the package was undeniably present
# and undeniably not in `uv.lock`. `UninstallBudgetGuardrail` was therefore
# unreachable through the only code path that fed it a plan: not merely
# "unexercised" as originally filed, but structurally dead code. Fixed here by
# giving the SAME guardrail machinery a plan source that can actually contain
# removals, computed without ever touching `uv sync` at all — so it can never
# produce the destructive bare-sync argv `_assert_sanctioned` exists to catch.
# ─────────────────────────────────────────────────────────────────────────────
def plan_prune(workspace: Workspace) -> PrunePlan:
    """Find installed distributions `--inexact` deliberately leaves behind.

    Read-only: compares `.dist-info` on disk against `uv.lock`'s package set
    and the workspace's own member names. Never invokes `uv sync`, so it sees
    exactly what a hand-audit of the venv would see, independent of any sync
    flag.
    """

    site = workspace.site_packages()
    if site is None or not site.is_dir():
        return PrunePlan(candidates=())
    installed = _installed_distributions(site)
    locked = _locked_distribution_names(workspace)
    member_names = {m.canonical for m in workspace.members()}
    candidates = tuple(
        PruneCandidate(name=record.name, version=record.version)
        for canon, record in sorted(installed.items())
        if canon not in locked and canon not in member_names
    )
    return PrunePlan(candidates=candidates)


def _prune_activity_gate(
    workspace: Workspace, ignore_activity: bool
) -> PruneOutcome | None:
    if ignore_activity:
        return None
    activity = detect_activity(workspace)
    if not activity:
        return None
    names = "; ".join(f"{r.probe}:{r.identifier}" for r in activity[:5])
    return PruneOutcome(
        plan=PrunePlan(candidates=()),
        applied=False,
        detail=f"deferred: environment busy ({names})",
    )


def _prune_budget_gate(plan: PrunePlan, allow_uninstalls: int) -> PruneOutcome | None:
    if len(plan.candidates) <= allow_uninstalls:
        return None
    names = ", ".join(c.name for c in plan.candidates[:8])
    return PruneOutcome(
        plan=plan,
        applied=False,
        refused=True,
        detail=(
            f"plan would remove {len(plan.candidates)} package(s) "
            f"({names}) but only {allow_uninstalls} are sanctioned for "
            "this operation; re-run with a larger --allow-uninstalls "
            "budget if this is intended"
        ),
    )


def _prune_apply(workspace: Workspace, plan: PrunePlan) -> PruneOutcome:
    python = workspace.venv / "bin" / "python"
    for candidate in plan.candidates:
        result = run_uv(
            workspace, ["pip", "uninstall", "--python", str(python), candidate.name]
        )
        if not result.ok:
            raise VenvSyncError(
                f"uv pip uninstall {candidate.name} failed "
                f"(rc={result.returncode}): {result.output[-2000:]}"
            )
    return PruneOutcome(
        plan=plan, applied=True, detail=f"removed {len(plan.candidates)} package(s)"
    )


def prune(
    workspace: Workspace,
    *,
    allow_uninstalls: int,
    apply: bool = True,
    ignore_activity: bool = False,
) -> PruneOutcome:
    """Remove up to `allow_uninstalls` packages that are installed but not
    locked and not a workspace member — the class `sync()` can never reach.

    Deliberately budgeted and explicit: `allow_uninstalls` has no default
    that removes anything (`prune(workspace, allow_uninstalls=0)` always
    refuses), matching `--allow-uninstalls`'s existing "any uninstall
    refuses by default" contract elsewhere in this module. Removes one
    package per `uv pip uninstall` call — never `uv sync` — so this path can
    never produce the destructive bare-sync argv `_assert_sanctioned` exists
    to catch, and a locked or member distribution can never appear as a
    candidate in the first place (excluded by construction in `plan_prune`).
    """

    if allow_uninstalls <= 0:
        raise VenvSyncError(
            "prune() refuses without an explicit allow_uninstalls > 0 budget "
            "(0 is the same as sync()'s default: any uninstall refuses)"
        )

    activity_outcome = _prune_activity_gate(workspace, ignore_activity)
    if activity_outcome is not None:
        return activity_outcome

    plan = plan_prune(workspace)
    if plan.is_empty:
        return PruneOutcome(plan=plan, applied=False, detail="nothing to prune")

    budget_outcome = _prune_budget_gate(plan, allow_uninstalls)
    if budget_outcome is not None:
        return budget_outcome

    if not apply:
        return PruneOutcome(
            plan=plan,
            applied=False,
            detail=f"plan approved but not applied (--dry-run): {len(plan.candidates)} removal(s)",
        )

    return _prune_apply(workspace, plan)


def verify(workspace: Workspace) -> tuple[ProbeResult, ...]:
    """Run every verify probe.  ``ok is False`` anywhere means "roll back"."""

    return tuple(probe.check(workspace) for probe in VERIFY_PROBES)


# ─────────────────────────────────────────────────────────────────────────────
# Editable members: source change vs metadata change
# ─────────────────────────────────────────────────────────────────────────────
SOURCE_ONLY = "source-only"
METADATA = "metadata"
NATIVE = "native"
LOCK = "lock"

_METADATA_FILENAMES = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "MANIFEST.in",
        "requirements.txt",
        "requirements-dev.txt",
    }
)
_NATIVE_FILENAMES = frozenset({"Cargo.toml", "Cargo.lock", "build.rs"})
_NATIVE_SUFFIXES = frozenset({".rs", ".c", ".h", ".pyx", ".pxd"})


def classify_change(paths: Sequence[str]) -> str:
    """Classify a merge's changed paths into the action it actually requires.

    This is the distinction the whole "flip on merge" feature turns on:

    * **source-only** — a ``.py`` edit inside an editable member is *already*
      live the instant the merge writes the file, for the member and for every
      one of its downstream dependents, because an editable install resolves
      imports through the source tree.  Reinstalling would be pure cost.
    * **metadata** — ``pyproject.toml``/``setup.cfg`` changes are baked into the
      installed ``.dist-info`` at install time.  A dependency change also
      invalidates the resolution shared by every dependent, so it needs a
      relock followed by a sync; an entry-point or version change needs the
      member reinstalled.
    * **native** — Rust/C sources need the extension rebuilt; an editable
      install does not track those.
    * **lock** — ``uv.lock`` itself moved, so the environment must be re-synced.

    The classification is the cheap *trigger-time* signal.  It is deliberately
    biased toward doing work: anything not recognised as source-only escalates.
    :func:`member_install_states` is the authoritative check that runs anyway.
    """

    verdicts = {SOURCE_ONLY}
    for raw in paths:
        name = Path(raw).name
        suffix = Path(raw).suffix
        if name == "uv.lock":
            verdicts.add(LOCK)
        elif name in _METADATA_FILENAMES:
            verdicts.add(METADATA)
        elif name in _NATIVE_FILENAMES or suffix in _NATIVE_SUFFIXES:
            verdicts.add(NATIVE)
    for level in (METADATA, NATIVE, LOCK):
        if level in verdicts:
            return level
    return SOURCE_ONLY


def _member_install_differences(
    record: _InstalledRecord,
    source_version: str | None,
    dynamic_version: bool,
    source_eps: tuple[str, ...],
) -> list[str]:
    differences: list[str] = []
    if not record.editable:
        differences.append("installed non-editable: source edits will NOT be live")
    if (
        not dynamic_version
        and source_version is not None
        and record.version != source_version
    ):
        differences.append(
            f"version {record.version} installed but {source_version} declared"
        )
    if set(source_eps) != set(record.entry_points):
        added = sorted(set(source_eps) - set(record.entry_points))
        removed = sorted(set(record.entry_points) - set(source_eps))
        differences.append(
            "console scripts differ"
            + (f"; missing {added}" if added else "")
            + (f"; stale {removed}" if removed else "")
        )
    return differences


def _member_install_state(
    member: Member, installed: dict[str, _InstalledRecord]
) -> MemberInstallState:
    source_version, source_eps, dynamic_version = _source_metadata(member.path)
    record = installed.get(member.canonical)
    if record is None:
        return MemberInstallState(
            member=member,
            installed=False,
            editable=False,
            source_version=source_version,
            installed_version=None,
            source_entry_points=source_eps,
            installed_entry_points=(),
            differences=("not installed into the shared venv",),
        )
    differences = _member_install_differences(
        record, source_version, dynamic_version, source_eps
    )
    return MemberInstallState(
        member=member,
        installed=True,
        editable=record.editable,
        source_version=source_version,
        installed_version=record.version,
        source_entry_points=source_eps,
        installed_entry_points=record.entry_points,
        differences=tuple(differences),
    )


def member_install_states(workspace: Workspace) -> tuple[MemberInstallState, ...]:
    """Compare every member's *source* metadata against its installed record.

    Scope note, deliberately narrow to avoid crying wolf: dependency drift is
    already detected exactly and cheaply by ``uv lock --check`` (a member whose
    ``dependencies`` moved makes the lock stale by definition).  Re-deriving a
    requirement-by-requirement diff here would only add a second, fuzzier answer
    to the same question.  What ``uv lock --check`` genuinely cannot see is
    metadata that does **not** affect resolution — a version bump, an added or
    renamed console script, a member that got installed non-editable — and those
    are exactly the fields this compares.
    """

    site = workspace.site_packages()
    installed = _installed_distributions(site) if site else {}
    return tuple(
        _member_install_state(member, installed) for member in workspace.members()
    )


def _installed_distributions(site: Path) -> dict[str, _InstalledRecord]:
    """Read ``.dist-info`` directly — we may not be running inside this venv."""

    records: dict[str, _InstalledRecord] = {}
    for dist_info in sorted(site.glob("*.dist-info")):
        metadata = dist_info / "METADATA"
        try:
            headers = _parse_rfc822(
                metadata.read_text(encoding="utf-8", errors="replace")
            )
        except OSError as exc:
            logger.warning("unreadable dist metadata %s: %s", metadata, exc)
            continue
        name = headers.get("name")
        if not name:
            continue
        records[canonical_name(name)] = _InstalledRecord(
            name=name,
            version=headers.get("version", ""),
            editable=_is_editable(dist_info),
            entry_points=_read_entry_points(dist_info / "entry_points.txt"),
        )
    return records


def _parse_rfc822(text: str) -> dict[str, str]:
    """Minimal single-valued header parse (stdlib ``email`` is overkill here)."""

    headers: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            break
        if ":" not in line or line[:1].isspace():
            continue
        key, _, value = line.partition(":")
        headers.setdefault(key.strip().lower(), value.strip())
    return headers


def _is_editable(dist_info: Path) -> bool:
    direct_url = dist_info / "direct_url.json"
    try:
        payload = json.loads(direct_url.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as exc:
        logger.warning("unreadable direct_url for %s: %s", dist_info.name, exc)
        return False
    return bool(payload.get("dir_info", {}).get("editable"))


def _read_entry_points(path: Path) -> tuple[str, ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except OSError as exc:
        logger.warning("unreadable entry points %s: %s", redact_path_for_log(path), exc)
        return ()
    scripts: list[str] = []
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section == "console_scripts" and "=" in stripped:
            scripts.append(stripped.split("=", 1)[0].strip())
    return tuple(sorted(scripts))


def _source_metadata(path: Path) -> tuple[str | None, tuple[str, ...], bool]:
    manifest = path / "pyproject.toml"
    try:
        document = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("unreadable member manifest %s: %s", manifest, exc)
        return None, (), True
    project = document.get("project", {})
    dynamic = project.get("dynamic", [])
    version = project.get("version")
    scripts = project.get("scripts", {})
    return (
        version if isinstance(version, str) else None,
        tuple(sorted(scripts)) if isinstance(scripts, dict) else (),
        isinstance(dynamic, list) and "version" in dynamic,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Drift detection
# ─────────────────────────────────────────────────────────────────────────────
def _drift_lock_finding(check: Any) -> DriftFinding:
    return DriftFinding(
        code="lock_current",
        severity="ok" if check.ok else "fail",
        detail=(
            "uv.lock is current with the workspace manifests"
            if check.ok
            else "uv.lock is STALE — a manifest moved without a relock"
        ),
        data={"detail": "" if check.ok else check.output[-1200:]},
    )


def _drift_env_findings(workspace: Workspace) -> list[DriftFinding]:
    """The env_current / env_extraneous findings from a sync plan -- only
    computed when the lock itself is current (see ``detect_drift``)."""
    findings: list[DriftFinding] = []
    try:
        plan = plan_sync(workspace)
    except VenvSyncError as exc:
        findings.append(
            DriftFinding(
                code="plan_unavailable",
                severity="fail",
                detail=f"could not compute a sync plan: {exc}",
            )
        )
        return findings
    findings.append(
        DriftFinding(
            code="env_current",
            severity="ok" if not plan.installs else "fail",
            detail=(
                "installed packages match uv.lock"
                if not plan.installs
                else (
                    f"{len(plan.installs)} package(s) are BEHIND the lock — "
                    "the environment is not running what the lock resolves"
                )
            ),
            data={"installs": [f"{d.name}=={d.version}" for d in plan.installs[:40]]},
        )
    )
    if plan.removals:
        findings.append(
            DriftFinding(
                code="env_extraneous",
                severity="warn",
                detail=(
                    f"{len(plan.removals)} installed package(s) are not in "
                    "the lock; --inexact leaves them alone deliberately"
                ),
                data={"packages": [d.name for d in plan.removals[:40]]},
            )
        )
    return findings


def _drift_member_metadata_finding(workspace: Workspace) -> DriftFinding:
    states = member_install_states(workspace)
    stale = [s for s in states if s.stale]
    return DriftFinding(
        code="member_metadata",
        severity="ok" if not stale else "warn",
        detail=(
            f"all {len(states)} workspace member(s) installed editable and current"
            if not stale
            else (
                f"{len(stale)} member(s) have metadata that differs from their "
                "source (version / console scripts / editability)"
            )
        ),
        data={"stale": {s.member.name: list(s.differences) for s in stale[:20]}},
    )


def _drift_pending_intent_finding(workspace: Workspace) -> DriftFinding | None:
    pending = _pending_intent_count(workspace)
    if not pending:
        return None
    return DriftFinding(
        code="pending_flips",
        severity="warn",
        detail=(
            f"{pending} merge flip(s) are queued and not yet applied "
            "(deferred because the environment was busy)"
        ),
        data={"count": pending},
    )


def detect_drift(workspace: Workspace, *, include_floor: bool = True) -> DriftReport:
    """Answer "is the shared venv still what the lock says it should be?".

    Runs read-only.  This is the check whose absence let the environment rot for
    ten days, so it is wired into ``arbora-doctor`` and into the merge
    reconciler rather than left as a command somebody has to remember.
    """

    findings: list[DriftFinding] = []

    if not workspace.venv.is_dir():
        findings.append(
            DriftFinding(
                code="venv_missing",
                severity="fail",
                detail=f"no virtualenv at {workspace.venv}",
            )
        )
        return DriftReport(findings=tuple(findings))

    check = lock_check(workspace)
    findings.append(_drift_lock_finding(check))

    if check.ok:
        findings.extend(_drift_env_findings(workspace))

    findings.append(_drift_member_metadata_finding(workspace))

    if include_floor:
        floor = SdkFloorProbe().check(workspace)
        findings.append(
            DriftFinding(
                code="mcp_sdk_floor",
                severity={True: "ok", False: "fail", None: "ok"}[floor.ok],
                detail=floor.detail or "mcp sdk floor probe returned no detail",
            )
        )

    pending_finding = _drift_pending_intent_finding(workspace)
    if pending_finding is not None:
        findings.append(pending_finding)

    return DriftReport(findings=tuple(findings))


def _pending_intent_count(workspace: Workspace) -> int:
    directory = workspace.state_dir / "intents"
    if not directory.is_dir():
        return 0
    return len(list(directory.glob("*.json")))


def session_start_hint(workspace: Workspace) -> str | None:
    """A near-zero-cost drift check, safe to run on every agent session start.

    CONCEPT:MERISTEM-OS.deployment.workspace-venv-reconciler (D-VS-6). ``detect_drift``
    is the full picture, but two of its three checks (``lock_check``,
    ``plan_sync``) spawn a real ``uv`` subprocess each — ~10s combined, an
    operator-visible latency this lane declined to impose on every session
    start by default. ``member_install_states`` is pure Python (reads
    dist-info + ``pyproject.toml`` off disk, no subprocess), so it is the one
    piece of drift detection cheap enough to run unconditionally. It also
    covers the single highest-signal case: an editable member whose *source*
    has moved (version, console scripts, editability) without a resync —
    exactly what went undetected for ten days before this lane existed.

    Returns ``None`` when there is nothing to say (clean, or the check itself
    could not run) so a hooked-up caller stays silent on the common case;
    never raises, so it can never turn a session-start hook into a failure.
    """
    try:
        if not workspace.venv.is_dir():
            return None
        stale = [s for s in member_install_states(workspace) if s.stale]
    except (OSError, VenvSyncError):  # best-effort session hint,
        # never the reason a session fails to start; caller sees silence.
        logger.debug("session_start_hint: drift check failed", exc_info=True)
        return None
    if not stale:
        return None
    names = ", ".join(sorted(s.member.name for s in stale)[:5])
    more = f" (+{len(stale) - 5} more)" if len(stale) > 5 else ""
    return (
        f"arbora-venv: {len(stale)} editable member(s) look stale in "
        f"the shared venv ({names}{more}) — run `arbora-venv status` "
        "for the full report, `arbora-venv sync` to reconcile."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Upgrade / relock / rollback
# ─────────────────────────────────────────────────────────────────────────────
def upgrade(
    workspace: Workspace,
    packages: Sequence[str] = (),
    *,
    all_packages: bool = False,
    reason: str = "manual upgrade",
    ignore_activity: bool = False,
    allow_uninstalls: int = 0,
    hold_lock: bool = True,
) -> UpgradeOutcome:
    """Move dependencies forward, sync, verify, and roll back on failure.

    One command does the whole loop the operator asked for — relock, verify,
    roll back — because the dangerous part of an upgrade is not the relock, it
    is discovering three days later that it half worked and having no way back.

    ``hold_lock=False`` is for callers that already hold the exclusive writer
    lock (the merge reconciler does).  ``flock`` is per open-file-description,
    so re-acquiring it from the same process would falsely report "busy".
    """

    if not packages and not all_packages:
        raise VenvSyncError(
            "upgrade needs at least one --package, or --all to move everything"
        )

    try:
        if hold_lock:
            with exclusive_lock(workspace):
                return _upgrade_locked(
                    workspace,
                    packages,
                    all_packages=all_packages,
                    reason=reason,
                    ignore_activity=ignore_activity,
                    allow_uninstalls=allow_uninstalls,
                )
        return _upgrade_locked(
            workspace,
            packages,
            all_packages=all_packages,
            reason=reason,
            ignore_activity=ignore_activity,
            allow_uninstalls=allow_uninstalls,
        )
    except LockBusyError as exc:
        return UpgradeOutcome(
            verdict=Verdict(decision=DEFER, guardrail="writer_lock", reason=str(exc)),
            backup=None,
            plan=None,
            probes=(),
            applied=False,
            rolled_back=False,
            detail=str(exc),
        )


def _upgrade_locked(
    workspace: Workspace,
    packages: Sequence[str],
    *,
    all_packages: bool,
    reason: str,
    ignore_activity: bool,
    allow_uninstalls: int,
) -> UpgradeOutcome:
    ctx = SyncContext(
        workspace=workspace,
        reason=reason,
        allow_uninstalls=allow_uninstalls,
        ignore_activity=ignore_activity,
        activity=detect_activity(workspace),
    )
    busy = ActivityGuardrail().evaluate(None, ctx)
    if busy is not None:
        return UpgradeOutcome(
            verdict=busy,
            backup=None,
            plan=None,
            probes=(),
            applied=False,
            rolled_back=False,
            detail=busy.reason,
        )

    store = LockBackupStore(workspace)
    backup = store.create(
        reason, meta={"packages": list(packages), "all": all_packages}
    )

    args = ["lock"]
    if all_packages:
        args.append("--upgrade")
    for name in packages:
        args.extend(["--upgrade-package", name])
    result = run_uv(workspace, args, timeout=3600.0)
    if not result.ok:
        store.restore(backup.id)
        return UpgradeOutcome(
            verdict=Verdict(
                decision=REFUSE,
                guardrail="relock",
                reason=f"uv lock failed: {result.output[-1200:]}",
            ),
            backup=backup,
            plan=None,
            probes=(),
            applied=False,
            rolled_back=True,
            detail="lock restored from backup after a failed relock",
        )

    outcome = sync(
        workspace,
        reason=f"{reason} (post-relock)",
        apply=True,
        ignore_activity=True,  # already gated above, inside the writer lock
        allow_uninstalls=allow_uninstalls,
        hold_lock=False,
    )
    if not outcome.verdict.allowed:
        store.restore(backup.id)
        return UpgradeOutcome(
            verdict=outcome.verdict,
            backup=backup,
            plan=outcome.plan,
            probes=(),
            applied=False,
            rolled_back=True,
            detail=(
                "guardrail vetoed the post-upgrade plan; uv.lock restored from "
                f"backup {backup.id}"
            ),
        )

    probes = verify(workspace)
    failed = [p for p in probes if p.ok is False]
    if failed:
        store.restore(backup.id)
        recovery = sync(
            workspace,
            reason="rollback re-sync",
            apply=True,
            ignore_activity=True,
            allow_uninstalls=allow_uninstalls,
            hold_lock=False,
        )
        return UpgradeOutcome(
            verdict=Verdict(
                decision=REFUSE,
                guardrail="verify",
                reason="; ".join(f"{p.name}: {p.detail}" for p in failed),
            ),
            backup=backup,
            plan=outcome.plan,
            probes=probes,
            applied=False,
            rolled_back=True,
            detail=(
                f"verification failed; uv.lock restored from {backup.id} and the "
                f"environment re-synced ({recovery.detail})"
            ),
        )

    store.mark_verified(backup.id)
    return UpgradeOutcome(
        verdict=Verdict(decision=ALLOW),
        backup=backup,
        plan=outcome.plan,
        probes=probes,
        applied=True,
        rolled_back=False,
        detail=outcome.detail,
    )


def rollback(
    workspace: Workspace,
    backup_id: str | None = None,
    *,
    ignore_activity: bool = False,
) -> UpgradeOutcome:
    """Restore an archived ``uv.lock`` and re-sync the environment onto it."""

    try:
        with exclusive_lock(workspace):
            store = LockBackupStore(workspace)
            ctx = SyncContext(
                workspace=workspace,
                reason="rollback",
                ignore_activity=ignore_activity,
                activity=detect_activity(workspace),
            )
            busy = ActivityGuardrail().evaluate(None, ctx)
            if busy is not None:
                return UpgradeOutcome(
                    verdict=busy,
                    backup=None,
                    plan=None,
                    probes=(),
                    applied=False,
                    rolled_back=False,
                    detail=busy.reason,
                )
            # Archive the pre-rollback state too, so a rollback is itself undoable.
            store.create("pre-rollback snapshot")
            restored = store.restore(backup_id)
            outcome = sync(
                workspace,
                reason=f"rollback to {restored.id}",
                apply=True,
                ignore_activity=True,
                hold_lock=False,
            )
            probes = verify(workspace)
            return UpgradeOutcome(
                verdict=outcome.verdict,
                backup=restored,
                plan=outcome.plan,
                probes=probes,
                applied=outcome.applied,
                rolled_back=True,
                detail=f"restored {restored.id}: {outcome.detail}",
            )
    except LockBusyError as exc:
        return UpgradeOutcome(
            verdict=Verdict(decision=DEFER, guardrail="writer_lock", reason=str(exc)),
            backup=None,
            plan=None,
            probes=(),
            applied=False,
            rolled_back=False,
            detail=str(exc),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Leases
# ─────────────────────────────────────────────────────────────────────────────
def acquire_lease(workspace: Workspace, owner: str, *, ttl: float, reason: str) -> Path:
    """Declare a window in which the environment must not be swapped."""

    directory = workspace.state_dir / "leases"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{re.sub(r'[^A-Za-z0-9_.-]', '-', owner)}.json"
    _atomic_write_text(
        path,
        json.dumps(
            {
                "owner": owner,
                "reason": reason,
                "acquired_at": datetime.now(UTC).isoformat(),
                "expires_at": time.time() + ttl,
                "pid": os.getpid(),
            },
            indent=2,
            sort_keys=True,
        ),
    )
    return path


def release_lease(workspace: Workspace, owner: str) -> bool:
    """Drop a lease early."""

    path = (
        workspace.state_dir
        / "leases"
        / f"{re.sub(r'[^A-Za-z0-9_.-]', '-', owner)}.json"
    )
    if not path.exists():
        return False
    _unlink_quietly(path)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    """The ``arbora-venv`` console entry point."""
    from arbora.deployment.venv_cli import run_cli

    return run_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
