"""The activity and verification probes a sync runs before and after it changes an environment (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import json
import logging
import os
import subprocess  # nosec B404 -- fixed-argv environment synchronization
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from arbora.deployment.venv_backup import (
    _unlink_quietly,
)
from arbora.deployment.venv_plan import (
    SyncPlan,
    VenvSyncError,
    Workspace,
    redact_path_for_log,
)

from .venv_sync_types import (
    CommandResult,
    ProbeResult,
)

logger = logging.getLogger(__name__)


class UnsafeInvocationError(VenvSyncError):
    """An argv that would destroy the environment was assembled.

    Raised by :func:`_assert_sanctioned`.  This is the last line of defence:
    it fires *before* ``subprocess`` runs, so the destructive command is never
    executed even if it was constructed by code outside this module.
    """


# ─────────────────────────────────────────────────────────────────────────────
# The sanctioned invocation — the destructive form is unrepresentable
# ─────────────────────────────────────────────────────────────────────────────
#: Flags that MUST be present on every ``uv sync`` this module runs.
#:
#: ``--locked``       never relock as a side effect of syncing; a manifest that
#:                    moved is an explicit ``upgrade``, not a silent swap.
#: ``--all-packages`` the workspace root declares no dependencies, so without
#:                    this the target set is empty and everything is pruned.
#: ``--inexact``      never uninstall packages outside the target set; other
#:                    lanes legitimately add packages to the shared venv.
SANCTIONED_SYNC_FLAGS: tuple[str, ...] = ("--locked", "--all-packages", "--inexact")


@dataclass(frozen=True)
class SyncInvocation:
    """The one sanctioned ``uv sync`` command line.

    There is intentionally no field that can remove a flag from
    :data:`SANCTIONED_SYNC_FLAGS`.  ``dry_run`` only ever *adds* ``--dry-run``.
    """

    dry_run: bool = False

    def argv(self, uv: str) -> list[str]:
        argv = [uv, "sync", *SANCTIONED_SYNC_FLAGS]
        if self.dry_run:
            argv.append("--dry-run")
        return argv


def _assert_sanctioned(argv: Sequence[str]) -> None:
    """Refuse to execute a ``uv sync`` missing any mandatory safety flag."""

    if "sync" not in argv:
        return
    missing = [flag for flag in SANCTIONED_SYNC_FLAGS if flag not in argv]
    if missing:
        raise UnsafeInvocationError(
            "refusing to run an unsanctioned `uv sync`: missing "
            f"{', '.join(missing)}. The shared workspace root declares no "
            "dependencies, so this invocation would prune every editable "
            "workspace member. The only sanctioned form is "
            f"`uv sync {' '.join(SANCTIONED_SYNC_FLAGS)}`."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Activity detection — "is another lane mid-test?"
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ActivityRecord:
    """Evidence that something else is using the shared environment."""

    probe: str
    identifier: str
    detail: str


@runtime_checkable
class ActivityProbe(Protocol):
    """Answers "is somebody else using this venv right now?"."""

    name: str

    def busy(self, workspace: Workspace) -> Sequence[ActivityRecord]:
        """Return evidence of in-flight work (empty when idle)."""


#: Program names that mean "a build or test is running here".
#:
#: Matched against argv *tokens*, never against the joined command line: a
#: ``bash -c '…pytest…'`` wrapper carries the whole script in a single token, so
#: substring matching flagged every shell that merely mentioned a test command.
#: The real ``pytest`` child is still detected on its own, so nothing is lost.
_TEST_MARKERS = frozenset(
    {
        "pytest",
        "py.test",
        "unittest",
        "tox",
        "nox",
        "pre-commit",
        "maturin",
        "cargo",
        "rustc",
        "meson",
        "ninja",
    }
)


#: ``uv`` subcommands that mutate or depend on a stable environment.
_UV_SUBCOMMANDS = frozenset({"run", "sync", "lock", "pip", "build", "venv"})


def _build_or_test_command(cmdline: Sequence[str]) -> str | None:
    """Name the build/test tool this argv runs, or ``None``."""

    tokens = [Path(token).name for token in cmdline]
    if not tokens:
        return None
    if tokens[0] in ("uv", "uvx") and len(tokens) > 1 and tokens[1] in _UV_SUBCOMMANDS:
        return f"uv {tokens[1]}"
    for token in tokens:
        if token in _TEST_MARKERS:
            return token
    return None


def _looks_like_interpreter_or_test_command(cmdline: Sequence[str]) -> bool:
    """Whether ``cmdline`` is plausibly python/test work, not merely an
    inherited environment (D-VS-9).

    ``VIRTUAL_ENV`` is inherited by every descendant of an activated shell, so
    an unrelated child of that shell (``tail -40``, ``ls``, an editor) reads as
    "busy" purely because its parent activated the venv — a live run showed
    exactly this with ``tail -40``. Require the executable itself to look like
    a Python interpreter/tool or a recognised build/test command before
    treating an inherited ``VIRTUAL_ENV`` as evidence of in-flight work; the
    ``cmdline[0].startswith(venv_bin)`` and lease probes are unaffected and
    still catch the real cases directly.
    """
    if not cmdline:
        return False
    exe = Path(cmdline[0]).name
    if exe.startswith("python") or exe in {"pip", "pip3"}:
        return True
    return _build_or_test_command(cmdline) is not None


class ProcessActivityProbe:
    """Detect live processes bound to the shared venv by reading ``/proc``.

    Three independent signals, because no single one is sufficient:

    * ``cmdline[0]`` under ``<venv>/bin`` — catches ``.venv/bin/pytest`` and
      ``.venv/bin/python`` (``/proc/<pid>/exe`` is useless here: uv's venv
      python is a symlink to the interpreter in uv's own python store, so the
      resolved exe never mentions the venv);
    * ``VIRTUAL_ENV`` in the process environment — catches an activated shell
      and anything it spawned;
    * a build/test command whose working directory sits inside the workspace or
      any sibling worktree — catches ``uv run pytest`` and ``cargo`` builds that
      neither activate nor name the venv.
    """

    name = "process"

    def __init__(self, extra_roots: Sequence[Path] = ()) -> None:
        self._extra_roots = tuple(Path(p) for p in extra_roots)

    def _process_activity_reason(
        self,
        entry: Path,
        cmdline: list[str],
        venv_bin: str,
        workspace: Workspace,
        roots: tuple[Path, ...],
    ) -> str | None:
        """Which (if any) of the three independent signals (see class
        docstring) marks this process as busy on ``workspace``'s venv."""
        if cmdline[0].startswith(venv_bin):
            return "executing from the shared venv"
        if _proc_environ_virtualenv(entry) == str(
            workspace.venv
        ) and _looks_like_interpreter_or_test_command(cmdline):
            return "VIRTUAL_ENV points at the shared venv (interpreter/test command)"
        tool = _build_or_test_command(cmdline)
        if tool is None:
            return None
        cwd = _read_proc_link(entry / "cwd")
        if cwd is not None and any(_is_within(cwd, root) for root in roots):
            return f"{tool} running in {cwd}"
        return None

    def busy(self, workspace: Workspace) -> Sequence[ActivityRecord]:
        proc = Path("/proc")
        if not proc.is_dir():
            return ()
        venv_bin = str(workspace.venv / "bin")
        roots = (workspace.root, *self._extra_roots)
        me = os.getpid()
        mine = _own_process_lineage()
        found: list[ActivityRecord] = []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == me or pid in mine:
                continue
            cmdline = _read_proc_list(entry / "cmdline")
            if not cmdline:
                continue
            reason = self._process_activity_reason(
                entry, cmdline, venv_bin, workspace, roots
            )
            if reason is None:
                continue
            found.append(
                ActivityRecord(
                    probe=self.name,
                    identifier=f"pid {pid}",
                    detail=f"{reason}: {' '.join(cmdline)[:160]}",
                )
            )
        return tuple(found)


class LeaseActivityProbe:
    """Honour explicit leases taken by cooperating lanes.

    The process probe cannot see work that is *about* to start, nor a lane that
    has paused between test commands but still needs a stable environment.  A
    lane can therefore declare a window explicitly::

        arbora-venv lease acquire --owner lane-x --ttl 3600

    Leases expire, so a crashed lane cannot wedge the reconciler forever.
    """

    name = "lease"

    def busy(self, workspace: Workspace) -> Sequence[ActivityRecord]:
        directory = workspace.state_dir / "leases"
        if not directory.is_dir():
            return ()
        now = time.time()
        found: list[ActivityRecord] = []
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning(
                    "ignoring unreadable lease %s: %s", redact_path_for_log(path), exc
                )
                continue
            expires = float(payload.get("expires_at", 0.0))
            if expires <= now:
                _unlink_quietly(path)
                continue
            found.append(
                ActivityRecord(
                    probe=self.name,
                    identifier=str(payload.get("owner", path.stem)),
                    detail=(
                        f"lease held until "
                        f"{datetime.fromtimestamp(expires, UTC).isoformat()}"
                        f": {payload.get('reason', 'no reason given')}"
                    ),
                )
            )
        return tuple(found)


ACTIVITY_PROBES: list[ActivityProbe] = [ProcessActivityProbe(), LeaseActivityProbe()]


def register_activity_probe(
    probe: ActivityProbe, *, replace_existing: bool = True
) -> None:
    """Add (or replace) an activity probe."""

    if replace_existing:
        ACTIVITY_PROBES[:] = [p for p in ACTIVITY_PROBES if p.name != probe.name]
    ACTIVITY_PROBES.append(probe)


def detect_activity(workspace: Workspace) -> tuple[ActivityRecord, ...]:
    """Run every registered activity probe and collect the evidence."""

    records: list[ActivityRecord] = []
    for probe in ACTIVITY_PROBES:
        try:
            records.extend(probe.busy(workspace))
        except OSError as exc:
            # A probe that cannot read the system must not make the reconciler
            # claim the environment is idle; surface it as activity so the
            # decision fails safe (defer) instead of silently proceeding.
            records.append(
                ActivityRecord(
                    probe=probe.name,
                    identifier="probe-error",
                    detail=f"probe could not determine idleness: {exc}",
                )
            )
    return tuple(records)


def _own_process_lineage() -> frozenset[int]:
    """PIDs of this process' ancestors, so we never defer to ourselves."""

    lineage: set[int] = set()
    pid = os.getpid()
    for _ in range(32):
        stat = Path("/proc") / str(pid) / "stat"
        try:
            fields = stat.read_text(encoding="utf-8", errors="replace").rsplit(") ", 1)
        except OSError:
            break
        if len(fields) != 2:
            break
        parts = fields[1].split()
        if len(parts) < 2 or not parts[1].isdigit():
            break
        pid = int(parts[1])
        if pid <= 1 or pid in lineage:
            break
        lineage.add(pid)
    return frozenset(lineage)


def _read_proc_list(path: Path) -> list[str]:
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    return [part for part in raw.decode("utf-8", "replace").split("\0") if part]


def _proc_environ_virtualenv(entry: Path) -> str | None:
    for item in _read_proc_list(entry / "environ"):
        if item.startswith("VIRTUAL_ENV="):
            return item.split("=", 1)[1]
    return None


def _read_proc_link(path: Path) -> Path | None:
    try:
        return Path(os.readlink(path))
    except OSError:
        return None


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# uv invocation
# ─────────────────────────────────────────────────────────────────────────────
def run_uv(
    workspace: Workspace, args: Sequence[str], *, timeout: float = 3600.0
) -> CommandResult:
    """Run uv in the workspace root, refusing unsanctioned sync invocations."""

    argv = [workspace.uv, *args]
    _assert_sanctioned(argv)
    logger.debug("running %s", " ".join(argv))
    try:
        completed = subprocess.run(  # nosec B603 -- reviewed argv, never shell
            argv,
            cwd=str(workspace.root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise VenvSyncError(
            f"uv executable not found at {workspace.uv!r}: {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise VenvSyncError(f"uv timed out after {timeout}s: {' '.join(argv)}") from exc
    return CommandResult(
        argv=tuple(argv),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def lock_check(workspace: Workspace) -> CommandResult:
    """``uv lock --check`` — is the lock current with the manifests?"""

    return run_uv(workspace, ["lock", "--check"], timeout=900.0)


def plan_sync(workspace: Workspace) -> SyncPlan:
    """Compute what the sanctioned sync would do, without touching anything."""

    result = run_uv(workspace, SyncInvocation(dry_run=True).argv(workspace.uv)[1:])
    if not result.ok:
        raise VenvSyncError(
            f"uv sync --dry-run failed (rc={result.returncode}): {result.output[-2000:]}"
        )
    return SyncPlan.parse(result.output)


# ─────────────────────────────────────────────────────────────────────────────
# Verify probes
# ─────────────────────────────────────────────────────────────────────────────
@runtime_checkable
class VerifyProbe(Protocol):
    """Asserts the environment is healthy after a change."""

    name: str

    def check(self, workspace: Workspace) -> ProbeResult:
        """Return ``ok=True``/``False``, or ``ok=None`` when inapplicable."""


class LockCheckProbe:
    """The lock still matches the manifests."""

    name = "lock_check"

    def check(self, workspace: Workspace) -> ProbeResult:
        result = lock_check(workspace)
        return ProbeResult(
            name=self.name,
            ok=result.ok,
            detail="uv.lock is current" if result.ok else result.output[-600:],
        )


class CleanPlanProbe:
    """The environment matches the lock exactly."""

    name = "clean_plan"

    def check(self, workspace: Workspace) -> ProbeResult:
        try:
            plan = plan_sync(workspace)
        except VenvSyncError as exc:
            return ProbeResult(name=self.name, ok=False, detail=str(exc))
        if plan.is_empty:
            return ProbeResult(
                name=self.name, ok=True, detail="environment matches the lock"
            )
        return ProbeResult(
            name=self.name,
            ok=False,
            detail=(
                f"{len(plan.installs)} install(s) and {len(plan.uninstalls)} "
                "uninstall(s) still outstanding"
            ),
        )


#: Modules imported to prove the environment is usable, not merely installed.
#: ``arbora.fleet.child_resilience`` is here for a specific reason: it is
#: the module whose silent ``ImportError`` under a stale ``fastmcp`` stopped an
#: entire test module from collecting and hid thirteen defects for ten days.
DEFAULT_IMPORT_PROBE_MODULES: tuple[str, ...] = (
    "meristem",
    "arbora.fleet.child_resilience",
)


class ImportProbe:
    """Import canary modules inside the shared venv's interpreter.

    A canary that is simply *not installed* in this environment is not a
    failure — it is inapplicable, and reported as ``ok=None``.  A canary that is
    installed but raises on import IS a failure, and that distinction is the
    entire value of the probe: the ten-day outage was the child-resilience
    surface present-but-unimportable against a stale ``fastmcp``,
    which reads as "installed and broken", not "absent".
    """

    name = "imports"

    def __init__(self, modules: Sequence[str] = DEFAULT_IMPORT_PROBE_MODULES) -> None:
        self.modules = tuple(modules)

    @staticmethod
    def _resolve_probe_python(workspace: Workspace) -> Path | None:
        python = workspace.venv / "bin" / "python"
        if not python.exists():
            python = workspace.venv / "Scripts" / "python.exe"
        if not python.exists():
            return None
        return python

    @staticmethod
    def _build_import_probe_script(modules: tuple[str, ...]) -> str:
        return "\n".join(
            [
                "import importlib, importlib.util, json, sys",
                "bad, absent = {}, []",
                f"for name in {list(modules)!r}:",
                "    top = name.split('.')[0]",
                "    try:",
                "        found = importlib.util.find_spec(top) is not None",
                "    except BaseException:",
                "        found = True",
                "    if not found:",
                "        absent.append(name)",
                "        continue",
                "    try:",
                "        importlib.import_module(name)",
                "    except BaseException as exc:",
                "        bad[name] = f'{type(exc).__name__}: {exc}'",
                "json.dump({'bad': bad, 'absent': absent}, sys.stdout)",
            ]
        )

    def _run_import_probe_subprocess(
        self, python: Path, workspace: Workspace, script: str
    ) -> tuple[Any, ProbeResult | None]:
        """Returns ``(completed, error)``; exactly one is falsy."""
        try:
            completed = subprocess.run(  # nosec B603 -- fixed Python executable/script
                [str(python), "-c", script],
                cwd=str(workspace.root),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, ProbeResult(
                name=self.name, ok=False, detail=f"import probe could not run: {exc}"
            )
        return completed, None

    def _parse_import_probe_output(
        self, completed: Any
    ) -> tuple[dict[str, str], list[str], ProbeResult | None]:
        """Returns ``(failures, absent, error)``; ``error`` is ``None`` on
        success."""
        try:
            payload = json.loads(completed.stdout or "{}")
            failures = dict(payload.get("bad", {}))
            absent = list(payload.get("absent", []))
        except (ValueError, TypeError) as exc:
            return (
                {},
                [],
                ProbeResult(
                    name=self.name,
                    ok=False,
                    detail=(
                        f"import probe produced unparsable output ({exc}): "
                        f"{(completed.stdout or completed.stderr)[-400:]}"
                    ),
                ),
            )
        return failures, absent, None

    def _import_probe_verdict(
        self, failures: dict[str, str], absent: list[str]
    ) -> ProbeResult:
        if failures:
            return ProbeResult(
                name=self.name,
                ok=False,
                detail="; ".join(f"{k} -> {v}" for k, v in sorted(failures.items())),
            )
        checked = len(self.modules) - len(absent)
        if not checked:
            return ProbeResult(
                name=self.name,
                ok=None,
                detail=f"no canary module is installed here (absent: {absent})",
            )
        return ProbeResult(
            name=self.name,
            ok=True,
            detail=(
                f"{checked} canary module(s) import cleanly"
                + (f"; not installed here: {absent}" if absent else "")
            ),
        )

    def check(self, workspace: Workspace) -> ProbeResult:
        python = self._resolve_probe_python(workspace)
        if python is None:
            return ProbeResult(
                name=self.name, ok=None, detail=f"no interpreter under {workspace.venv}"
            )
        script = self._build_import_probe_script(self.modules)

        completed, error = self._run_import_probe_subprocess(python, workspace, script)
        if error is not None:
            return error

        failures, absent, error = self._parse_import_probe_output(completed)
        if error is not None:
            return error

        return self._import_probe_verdict(failures, absent)


class SdkFloorProbe:
    """Reuse ``check_mcp_sdk_floor`` rather than re-deriving MCP SDK floors.

    A sibling lane already built the "derive the required floor from installed
    metadata, compare against what is installed" check.  Duplicating that logic
    here would give us two answers that can disagree, so this probe simply calls
    it inside the shared venv when it is available and reports ``ok=None`` when
    the running tree does not have it.
    """

    name = "mcp_sdk_floor"

    def check(self, workspace: Workspace) -> ProbeResult:
        python = workspace.venv / "bin" / "python"
        if not python.exists():
            return ProbeResult(name=self.name, ok=None, detail="no venv interpreter")
        script = (
            "import json, sys\n"
            "try:\n"
            "    from arbora.fleet.protocol_compat import check_mcp_sdk_floor\n"
            "except Exception as exc:\n"
            "    json.dump({'ok': None, 'detail': f'unavailable: {type(exc).__name__}: {exc}'}, sys.stdout)\n"
            "else:\n"
            "    json.dump(check_mcp_sdk_floor(), sys.stdout)\n"
        )
        try:
            completed = subprocess.run(  # nosec B603 -- fixed Python executable/script
                [str(python), "-c", script],
                cwd=str(workspace.root),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            payload = json.loads(completed.stdout or "{}")
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return ProbeResult(
                name=self.name, ok=None, detail=f"floor probe could not run: {exc}"
            )
        return ProbeResult(
            name=self.name,
            ok=payload.get("ok"),
            detail=str(payload.get("detail", "")),
        )


VERIFY_PROBES: list[VerifyProbe] = [
    LockCheckProbe(),
    CleanPlanProbe(),
    ImportProbe(),
    SdkFloorProbe(),
]


def register_verify_probe(probe: VerifyProbe, *, replace_existing: bool = True) -> None:
    """Add (or replace) a verification probe."""

    if replace_existing:
        VERIFY_PROBES[:] = [p for p in VERIFY_PROBES if p.name != probe.name]
    VERIFY_PROBES.append(probe)
