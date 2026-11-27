"""Workspace discovery and the sync plan: the members of a uv workspace and the package changes one sync would make (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def redact_path_for_log(path: object) -> str:
    """Short, deterministic, non-reversible tag for a path in a log line.

    CONCEPT:MERISTEM-OS.observability.log-location-privacy — a raw filesystem path in
    a log line leaks host layout to anyone with log read access. Stdlib-only
    (this module's own "must not import anything the venv provides" constraint
    rules out the shared ``arbora.security.log_redaction`` helper);
    the same input always hashes to the same tag within a process, so repeated
    log lines about the same path can still be correlated without the literal
    value ever appearing.
    """
    if not path:
        return "<empty>"
    digest = hashlib.sha256(str(path).encode("utf-8", errors="replace")).hexdigest()
    return f"<redacted:{digest[:12]}>"


# ─────────────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────────────
class VenvSyncError(RuntimeError):
    """Base class for every failure this module raises."""


class WorkspaceNotFoundError(VenvSyncError):
    """No ancestor directory declares ``[tool.uv.workspace]``."""


class PlanParseError(VenvSyncError):
    """``uv sync --dry-run`` output could not be parsed unambiguously.

    Deliberately fatal.  A plan we cannot read is a plan we cannot police, and
    the failure mode we are guarding against is precisely "silently uninstall
    more than expected", so the safe answer is to refuse rather than to guess.
    """


# ─────────────────────────────────────────────────────────────────────────────
# Workspace discovery
# ─────────────────────────────────────────────────────────────────────────────
def canonical_name(name: str) -> str:
    """PEP 503 normalised distribution name."""

    return re.sub(r"[-_.]+", "-", name.strip()).lower()


@dataclass(frozen=True)
class Member:
    """One uv workspace member, resolved from the root manifest."""

    path: Path
    name: str

    @property
    def canonical(self) -> str:
        return canonical_name(self.name)


@dataclass(frozen=True)
class Workspace:
    """A uv workspace root plus the derived paths this module operates on."""

    root: Path
    uv: str = "uv"
    state_dir: Path = field(default_factory=lambda: Path())

    @property
    def pyproject(self) -> Path:
        return self.root / "pyproject.toml"

    @property
    def lock(self) -> Path:
        return self.root / "uv.lock"

    @property
    def venv(self) -> Path:
        return self.root / ".venv"

    @classmethod
    def discover(
        cls,
        start: Path | str | None = None,
        *,
        uv: str | None = None,
        state_dir: Path | None = None,
    ) -> Workspace:
        """Walk up from ``start`` to the nearest ``[tool.uv.workspace]`` root.

        Never shells out to git: the workspace root (the parent of all repo
        checkouts, e.g. ``${WORKSPACE_ROOT}``) is not itself a repository,
        which is exactly why the pre-existing ``scripts/uv_workspace.py``
        launcher cannot run there.
        """

        origin = Path(start or Path.cwd()).resolve()
        candidates = (origin, *origin.parents) if origin.is_dir() else origin.parents
        for candidate in candidates:
            manifest = candidate / "pyproject.toml"
            if not manifest.is_file():
                continue
            try:
                document = tomllib.loads(manifest.read_text(encoding="utf-8"))
            except (
                OSError,
                tomllib.TOMLDecodeError,
            ) as exc:  # deliberate DEBUG: workspace discovery walks EVERY ancestor directory, and an unreadable or non-uv pyproject.toml along that path is expected, not exceptional; it simply is not the workspace root. Warning here would fire on ordinary trees. The cause is preserved (interpolated) and the walk continues.
                logger.debug("skipping unreadable manifest %s: %s", manifest, exc)
                continue
            workspace = document.get("tool", {}).get("uv", {}).get("workspace")
            if isinstance(workspace, dict):
                resolved_uv = uv or shutil.which("uv") or "uv"
                root = candidate.resolve()
                return cls(
                    root=root,
                    uv=resolved_uv,
                    state_dir=state_dir or default_state_dir(root),
                )
        raise WorkspaceNotFoundError(
            f"no ancestor of {origin} declares [tool.uv.workspace]; "
            "pass the workspace root explicitly with --workspace"
        )

    def members(self) -> tuple[Member, ...]:
        """Resolve ``members`` minus ``exclude`` from the root manifest."""

        try:
            document = tomllib.loads(self.pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise VenvSyncError(
                f"workspace manifest {self.pyproject} is unreadable: {exc}"
            ) from exc
        config = document.get("tool", {}).get("uv", {}).get("workspace", {})
        included = _expand_member_patterns(self.root, config.get("members", []))
        excluded = _expand_member_patterns(self.root, config.get("exclude", []))
        resolved: list[Member] = []
        for path in sorted(included - excluded):
            name = _project_name(path)
            if name is not None:
                resolved.append(Member(path=path, name=name))
        return tuple(resolved)

    def site_packages(self) -> Path | None:
        """Locate the venv's ``site-packages`` without importing from it."""

        for pattern in ("lib/python*/site-packages", "Lib/site-packages"):
            for match in sorted(self.venv.glob(pattern)):
                if match.is_dir():
                    return match
        return None


def _expand_member_patterns(root: Path, patterns: Any) -> set[Path]:
    if not isinstance(patterns, list) or any(
        not isinstance(item, str) for item in patterns
    ):
        raise VenvSyncError(
            "tool.uv.workspace members/exclude must be lists of path patterns"
        )
    resolved: set[Path] = set()
    for pattern in patterns:
        for match in sorted(root.glob(pattern)):
            if match.is_dir() and (match / "pyproject.toml").is_file():
                resolved.add(match.resolve())
    return resolved


def _project_name(path: Path) -> str | None:
    manifest = path / "pyproject.toml"
    try:
        document = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning(
            "workspace member %s has an unreadable manifest: %s",
            redact_path_for_log(path),
            exc,
        )
        return None
    name = document.get("project", {}).get("name")
    return name if isinstance(name, str) and name else None


def default_state_dir(root: Path) -> Path:
    """Per-workspace state under ``~/.local/state``, keyed by the root path.

    State lives outside the workspace on purpose: the lock backups and the
    pending-flip queue must survive ``rm -rf .venv`` and must never be something
    a lane can accidentally commit.

    The location is deliberately **not** configurable and reads no environment
    variable.  A git hook, a detached reconciler and an interactive shell run
    with three different environments; if the path moved with ``XDG_STATE_HOME``
    they would each see a different queue and a different backup set, and a
    flip enqueued by the hook would be invisible to the drain.  One fixed
    location is the property that makes the queue coherent.
    """

    base = Path.home() / ".local" / "state"
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    return base / "arbora" / "venv-autosync" / f"{root.name}-{digest}"


# ─────────────────────────────────────────────────────────────────────────────
# Plan parsing
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PackageDelta:
    """One package uv would install, uninstall or replace."""

    name: str
    version: str = ""
    source: str | None = None
    url: str | None = None

    @property
    def canonical(self) -> str:
        return canonical_name(self.name)

    @property
    def is_local(self) -> bool:
        for candidate in (self.source, self.url):
            if candidate and candidate.startswith("file://"):
                return True
        return False


_HEADER_RE = re.compile(
    r"^Would (?P<verb>install|uninstall|update|downgrade) (?P<count>\d+) packages?$"
)


#: uv writes registry packages as ``name==version`` and local/direct-URL ones as
#: ``name @ file:///path`` — an editable member's *install* line uses the second
#: form while its *uninstall* line uses the first, so both must parse or a
#: rebuild looks like a pure removal.
_ENTRY_RE = re.compile(
    r"^\s*(?P<sign>[-+~])\s+(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:==(?P<version>[^\s(]+)|\s+@\s+(?P<url>\S+))"
    r"(?:\s+\((?:from\s+)?(?P<source>[^)]*)\))?\s*$"
)


_NO_CHANGES_RE = re.compile(r"^Would make no changes$")


@dataclass(frozen=True)
class SyncPlan:
    """What ``uv sync --dry-run`` says it would do to the environment."""

    installs: tuple[PackageDelta, ...] = ()
    uninstalls: tuple[PackageDelta, ...] = ()
    no_changes: bool = False
    raw: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.installs and not self.uninstalls

    @property
    def removals(self) -> tuple[PackageDelta, ...]:
        """Uninstalls that are NOT paired with a reinstall of the same package.

        uv renders a *replacement* as an uninstall line plus an install line —
        which is what every editable member looks like whenever its metadata is
        rebuilt.  Treating those as removals would make the guardrails refuse
        the correct, sanctioned sync (observed end-to-end: a dependency added to
        a member produced ``- demo==1.0.0`` / ``+ demo @ file://…``).  Only the
        *net* removals are the dangerous ones the guardrails care about.
        """

        reinstalled = {delta.canonical for delta in self.installs}
        return tuple(d for d in self.uninstalls if d.canonical not in reinstalled)

    @property
    def replacements(self) -> tuple[PackageDelta, ...]:
        """Packages uv would uninstall and immediately reinstall."""

        reinstalled = {delta.canonical for delta in self.installs}
        return tuple(d for d in self.uninstalls if d.canonical in reinstalled)

    @classmethod
    def parse(cls, text: str) -> SyncPlan:
        """Parse uv's dry-run report, failing closed on the dangerous direction.

        uv prints a ``Would <verb> N packages`` header followed by ``N``
        ``+``/``-``/``~`` entry lines.  The invariant that must hold for this
        module to be trustworthy is that we never *under-count uninstalls*: the
        whole guardrail rests on being able to say "this plan removes 557
        packages" with certainty.  So the uninstall header is checked against
        the entries parsed and a shortfall raises.  Install counts are not
        validated, because uv formats replacements differently across versions
        and a false alarm there would block legitimate syncs for no safety gain.
        """

        installs: list[PackageDelta] = []
        uninstalls: list[PackageDelta] = []
        declared_uninstalls = 0
        no_changes = False

        for line in text.splitlines():
            header = _HEADER_RE.match(line.strip())
            if header:
                if header.group("verb") == "uninstall":
                    declared_uninstalls += int(header.group("count"))
                continue
            if _NO_CHANGES_RE.match(line.strip()):
                no_changes = True
                continue
            entry = _ENTRY_RE.match(line)
            if not entry:
                continue
            delta = PackageDelta(
                name=entry.group("name"),
                version=entry.group("version") or "",
                source=entry.group("source"),
                url=entry.group("url"),
            )
            if entry.group("sign") == "-":
                uninstalls.append(delta)
            else:
                installs.append(delta)

        if len(uninstalls) < declared_uninstalls:
            raise PlanParseError(
                f"uv declared {declared_uninstalls} uninstall(s) but only "
                f"{len(uninstalls)} could be parsed; refusing to act on a plan "
                "whose removals cannot be enumerated exactly"
            )
        return cls(
            installs=tuple(installs),
            uninstalls=tuple(uninstalls),
            no_changes=no_changes,
            raw=text,
        )
