"""Install, status and remove one harness plugin (ARBORA-HARNESS-PLUGINS-R002).

The install lifecycle is one explicit machine (:data:`INSTALL_EDGES`). The
record lives in ``<arbora config>/plugins.json``: the endpoint, the declared
skills, and per harness the digest, the place and the files arbora placed.
Install never edits a harness file it did not write: a hand-written door entry
or a hand-made door skill link refuses ``DUPLICATE_DOOR`` and prints the exact
removal step (owner decision D6). The claude-code place is a local marketplace
(owner decision D1) whose single entry runs ``arbora plugin bundle``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404 -- fixed-argv harness plugin commands
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from arbora.harness_plugins.compose import DOOR_SKILLS, PLUGIN_NAME
from arbora.harness_plugins.profiles import HarnessProfile

MARKETPLACE = "arbora-local"
BUNDLE_COMMAND = "arbora plugin bundle --harness claude-code --print-path"


class InstallState(StrEnum):
    ABSENT = "absent"
    GENERATED = "generated"
    INSTALLED = "installed"
    STALE = "stale"
    REMOVED = "removed"


class InstallEvent(StrEnum):
    GENERATE = "generate"
    INSTALL = "install"
    DRIFT = "drift"
    REMOVE = "remove"


_S, _E = InstallState, InstallEvent
INSTALL_EDGES: dict[tuple[InstallState, InstallEvent], InstallState] = {
    (_S.ABSENT, _E.GENERATE): _S.GENERATED,
    (_S.GENERATED, _E.INSTALL): _S.INSTALLED,
    (_S.INSTALLED, _E.DRIFT): _S.STALE,
    (_S.STALE, _E.INSTALL): _S.INSTALLED,
    (_S.INSTALLED, _E.REMOVE): _S.REMOVED,
    (_S.STALE, _E.REMOVE): _S.REMOVED,
    (_S.REMOVED, _E.GENERATE): _S.GENERATED,
}


class IllegalInstallTransition(RuntimeError):
    code = "INSTALL_TRANSITION_ILLEGAL"

    def __init__(self, state: InstallState, event: InstallEvent) -> None:
        super().__init__(f"{self.code}: {state} --{event}-->")
        self.state = state
        self.event = event


def transition(state: InstallState, event: InstallEvent) -> InstallState:
    target = INSTALL_EDGES.get((state, event))
    if target is None:
        raise IllegalInstallTransition(state, event)
    return target


@dataclass
class InstallRecord:
    harness: str
    digest: str
    place: str
    state: InstallState
    files: list[str] = field(default_factory=list)


@dataclass
class PluginsConfig:
    endpoint: str = ""
    skills: list[str] = field(default_factory=list)
    installs: dict[str, InstallRecord] = field(default_factory=dict)


def config_path() -> Path:
    from arbora.core.settings import config_dir

    return config_dir() / "plugins.json"


def load_config(path: Path | None = None) -> PluginsConfig:
    path = path or config_path()
    if not path.is_file():
        return PluginsConfig()
    raw = json.loads(path.read_text(encoding="utf-8"))
    installs = {
        name: InstallRecord(**{**rec, "state": InstallState(rec["state"])})
        for name, rec in (raw.get("installs") or {}).items()
    }
    return PluginsConfig(
        endpoint=str(raw.get("endpoint") or ""),
        skills=list(raw.get("skills") or []),
        installs=installs,
    )


def save_config(config: PluginsConfig, path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(config)
    for rec in data["installs"].values():
        rec["state"] = str(rec["state"])
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ── Duplicate door detection (R002.3) ───────────────────────────────────────
@dataclass(frozen=True)
class DuplicateDoor:
    path: str
    entry: str
    removal: str


_LINE_COMMENT = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', re.S)


def _read_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonc":
        text = _LINE_COMMENT.sub(lambda m: m.group(1) or "", text)
        text = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        return json.loads(text)
    except ValueError:
        return None


def _is_door(name: str, entry: Any, endpoint: str) -> bool:
    url = str(entry.get("url") or "") if isinstance(entry, dict) else ""
    return name == PLUGIN_NAME or (
        bool(endpoint) and url.rstrip("/") == endpoint.rstrip("/") + "/mcp"
    )


def _json_doors(path: Path, endpoint: str) -> list[tuple[str, str]]:
    data = _read_json(path)
    if not isinstance(data, dict):
        return []
    found = []
    tables: list[tuple[str, Any]] = [
        ("mcpServers", data.get("mcpServers")),
        ("mcp", data.get("mcp")),
    ]
    for project, body in (data.get("projects") or {}).items():
        if isinstance(body, dict):
            tables.append((f"projects[{project}].mcpServers", body.get("mcpServers")))
    for label, table in tables:
        for name, entry in (table or {}).items() if isinstance(table, dict) else ():
            if _is_door(name, entry, endpoint):
                found.append((label, name))
    return found


def find_duplicate_doors(
    profile: HarnessProfile,
    home: Path,
    endpoint: str,
    project_dirs: Sequence[Path] = (),
) -> list[DuplicateDoor]:
    """Every hand-written door entry and door skill link the profile could see."""
    doors: list[DuplicateDoor] = []
    configs = [home / rel for rel in profile.door_configs]
    configs += [
        d / name
        for d in project_dirs
        for name in ("opencode.json", "opencode.jsonc", ".mcp.json")
    ]
    for path in configs:
        if not path.is_file():
            continue
        if path.suffix == ".toml":
            text = path.read_text(encoding="utf-8")
            if re.search(rf"^\[mcp_servers\.{PLUGIN_NAME}\]", text, re.M):
                doors.append(
                    DuplicateDoor(
                        str(path),
                        f"[mcp_servers.{PLUGIN_NAME}]",
                        f"delete the [mcp_servers.{PLUGIN_NAME}] table from {path}",
                    )
                )
            continue
        for label, name in _json_doors(path, endpoint):
            doors.append(
                DuplicateDoor(
                    str(path),
                    f"{label}.{name}",
                    f'delete the "{name}" entry under {label} in {path}',
                )
            )
    for rel in profile.skill_dirs:
        for skill in DOOR_SKILLS:
            link = home / rel / skill
            if link.is_symlink() or link.exists():
                doors.append(
                    DuplicateDoor(str(link), f"skill {skill}", f"rm -r {link}")
                )
    return doors


# ── Placement ───────────────────────────────────────────────────────────────
Runner = Callable[[Sequence[str]], int]


def run_harness_cli(argv: Sequence[str]) -> int:
    return subprocess.run(  # nosec B603 -- argv is a fixed harness command
        list(argv), check=False, timeout=120
    ).returncode


def marketplace_root() -> Path:
    from arbora.core.settings import config_dir

    return config_dir() / "plugins" / "marketplace"


def marketplace_document() -> dict[str, Any]:
    return {
        "name": MARKETPLACE,
        "owner": {"name": "arbora"},
        "description": "The arbora door, generated on this machine by arbora plugin install.",
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "description": "The arbora door for this harness.",
                "source": {
                    "source": "command",
                    "command": BUNDLE_COMMAND,
                    "mode": "copy",
                    "timeout": 120,
                },
            }
        ],
    }


def place_claude_code(record: InstallRecord, run: Runner) -> list[str]:
    """Write the local marketplace and register it with the harness (R005.3)."""
    root = marketplace_root()
    path = root / ".claude-plugin" / "marketplace.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(marketplace_document(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for argv in (
        ["claude", "plugin", "marketplace", "add", str(root)],
        ["claude", "plugin", "install", f"{PLUGIN_NAME}@{MARKETPLACE}"],
    ):
        if run(argv) != 0:
            raise RuntimeError(f"harness command failed: {' '.join(argv)}")
    record.place = str(root)
    return [str(path)]


def plugin_dir(profile: HarnessProfile, home: Path) -> Path:
    """The harness's own plugin discovery directory for the copied tree."""
    import os

    if profile.id == "opencode":
        base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "opencode"
        return base / "plugins" / PLUGIN_NAME
    return home / f".{profile.id}" / "plugins" / PLUGIN_NAME


def place_tree(
    profile: HarnessProfile, tree: Path, home: Path, record: InstallRecord
) -> list[str]:
    target = plugin_dir(profile, home)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(tree, target)
    record.place = str(target)
    return sorted(str(p) for p in target.rglob("*") if p.is_file())


def unplace(record: InstallRecord, run: Runner) -> None:
    """Delete only what arbora placed."""
    if record.harness == "claude-code":
        run(["claude", "plugin", "uninstall", f"{PLUGIN_NAME}@{MARKETPLACE}"])
        run(["claude", "plugin", "marketplace", "remove", MARKETPLACE])
    for name in record.files:
        Path(name).unlink(missing_ok=True)
    place = Path(record.place)
    for directory in sorted(
        (p for p in [place, *place.rglob("*")] if p.is_dir()), reverse=True
    ):
        if directory.exists() and not any(directory.iterdir()):
            directory.rmdir()


__all__ = [
    "INSTALL_EDGES",
    "DuplicateDoor",
    "IllegalInstallTransition",
    "InstallEvent",
    "InstallRecord",
    "InstallState",
    "PluginsConfig",
    "find_duplicate_doors",
    "load_config",
    "marketplace_document",
    "place_claude_code",
    "place_tree",
    "save_config",
    "transition",
    "unplace",
]
