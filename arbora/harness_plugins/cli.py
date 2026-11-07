"""``arbora plugin`` (ARBORA-HARNESS-PLUGINS-R001.4, R002.2, R003.2, R005.4, R007).

Sub-commands: ``bundle``, ``install``, ``status``, ``remove``, ``login``, ``hook``
and ``statusline``. :func:`arbora.console.main` dispatches here before any
server configuration loads. A refusal exits 2 and writes no file; ``hook``
always exits 0.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import webbrowser
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

from arbora.harness_plugins import install as inst
from arbora.harness_plugins.compose import AgentDef, SkillMissing, compose_bundle
from arbora.harness_plugins.profiles import Lifecycle, profile, profile_ids
from arbora.harness_plugins.render import TreeRefused, render


class BundleRefusal(StrEnum):
    HARNESS_UNKNOWN = "HARNESS_UNKNOWN"
    ENDPOINT_UNSET = "ENDPOINT_UNSET"
    SKILL_UNKNOWN = "SKILL_UNKNOWN"
    DUPLICATE_DOOR = "DUPLICATE_DOOR"
    TREE_REFUSED = "TREE_REFUSED"


class Refused(Exception):
    def __init__(self, code: BundleRefusal, detail: str = "") -> None:
        super().__init__(f"{code.value}: {detail}" if detail else code.value)
        self.code = code


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def _cache_root() -> Path:
    return (
        Path(os.environ.get("XDG_CACHE_HOME") or _home() / ".cache")
        / "arbora"
        / "plugins"
    )


def _endpoint(arg: str | None, config: inst.PluginsConfig) -> str:
    value = arg or config.endpoint or os.environ.get("ARBORA_PUBLIC_URL", "")
    if not value.startswith(("http://", "https://")):
        raise Refused(BundleRefusal.ENDPOINT_UNSET)
    return value.rstrip("/")


def _skill_roots(args: argparse.Namespace) -> list[Path] | None:
    roots = list(args.skills_root or [])
    env = os.environ.get("ARBORA_PLUGIN_SKILL_ROOTS", "")
    roots += [Path(p) for p in env.split(os.pathsep) if p]
    return roots or None


def _agents(config_path: Path) -> list[AgentDef]:
    if not config_path.is_file():
        return []
    raw = json.loads(config_path.read_text(encoding="utf-8")).get("agents") or []
    return [
        AgentDef(name=a["name"], description=a["description"], profile_id=a["profile"])
        for a in raw
    ]


def build(
    harness: str, args: argparse.Namespace, config: inst.PluginsConfig
) -> tuple[str, Path]:
    """Compose and render one tree; returns ``(digest, tree path)``."""
    try:
        prof = profile(harness)
    except KeyError as exc:
        raise Refused(BundleRefusal.HARNESS_UNKNOWN, harness) from exc
    endpoint = _endpoint(getattr(args, "endpoint", None), config)
    try:
        spec = compose_bundle(
            prof,
            base_url=endpoint,
            skills=[*config.skills, *(getattr(args, "skill", None) or [])],
            agents=_agents(inst.config_path()),
            skill_roots=_skill_roots(args),
        )
    except SkillMissing as exc:
        raise Refused(BundleRefusal.SKILL_UNKNOWN, str(exc)) from exc
    out = getattr(args, "out", None)
    staging = Path(tempfile.mkdtemp(prefix="arbora-plugin-"))
    try:
        digest = render(spec, staging / "tree")
    except TreeRefused as exc:
        raise Refused(BundleRefusal.TREE_REFUSED, str(exc)) from exc
    target = Path(out) if out else _cache_root() / harness / digest
    if not (target / prof.manifest).is_file() or out:
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        (staging / "tree").rename(target)
    shutil.rmtree(staging, ignore_errors=True)
    return digest, target.resolve()


def bundle_command(args: argparse.Namespace) -> int:
    digest, path = build(args.harness, args, inst.load_config())
    print(
        str(path)
        if args.print_path
        else json.dumps({"harness": args.harness, "digest": digest, "path": str(path)})
    )
    return 0


def install_command(args: argparse.Namespace) -> int:
    config = inst.load_config()
    if args.endpoint:
        config.endpoint = args.endpoint.rstrip("/")
    prof = profile(args.harness) if args.harness in profile_ids() else None
    if prof is None:
        raise Refused(BundleRefusal.HARNESS_UNKNOWN, args.harness)
    endpoint = _endpoint(None, config)
    doors = inst.find_duplicate_doors(prof, _home(), endpoint, [Path.cwd()])
    if doors:
        print(
            f"{BundleRefusal.DUPLICATE_DOOR.value}: remove each entry, then rerun install.",
            file=sys.stderr,
        )
        for door in doors:
            print(f"  {door.entry} in {door.path}: {door.removal}", file=sys.stderr)
        return 2
    digest, tree = build(args.harness, args, config)
    previous = config.installs.get(args.harness)
    state = previous.state if previous else inst.InstallState.ABSENT
    if state in (inst.InstallState.ABSENT, inst.InstallState.REMOVED):
        state = inst.transition(state, inst.InstallEvent.GENERATE)
    elif state is inst.InstallState.INSTALLED:
        state = inst.transition(state, inst.InstallEvent.DRIFT)
    record = inst.InstallRecord(
        harness=args.harness, digest=digest, place="", state=state
    )
    if args.harness == "claude-code":
        record.files = inst.place_claude_code(record, inst.run_harness_cli)
    else:
        record.files = inst.place_tree(prof, tree, _home(), record)
    record.state = inst.transition(state, inst.InstallEvent.INSTALL)
    config.installs[args.harness] = record
    inst.save_config(config)
    print(
        json.dumps(
            {
                "harness": args.harness,
                "state": record.state.value,
                "digest": digest,
                "place": record.place,
            }
        )
    )
    return 0


def status_command(args: argparse.Namespace) -> int:
    config = inst.load_config()
    rows = {}
    for harness in args.harness or profile_ids():
        record = config.installs.get(harness)
        if record is None:
            rows[harness] = {"state": inst.InstallState.ABSENT.value}
            continue
        if record.state is inst.InstallState.INSTALLED:
            fresh, _ = build(
                harness,
                argparse.Namespace(
                    out=None, endpoint=None, skills_root=args.skills_root, skill=None
                ),
                config,
            )
            if fresh != record.digest:
                record.state = inst.transition(record.state, inst.InstallEvent.DRIFT)
        rows[harness] = {
            "state": record.state.value,
            "digest": record.digest,
            "place": record.place,
        }
    inst.save_config(config)
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


def remove_command(args: argparse.Namespace) -> int:
    config = inst.load_config()
    record = config.installs.get(args.harness)
    if record is None or record.state is inst.InstallState.REMOVED:
        print(json.dumps({"harness": args.harness, "state": "absent"}))
        return 0
    inst.unplace(record, inst.run_harness_cli)
    record.state = inst.transition(record.state, inst.InstallEvent.REMOVE)
    record.files = []
    inst.save_config(config)
    print(json.dumps({"harness": args.harness, "state": record.state.value}))
    return 0


def login_command(args: argparse.Namespace) -> int:
    from arbora.harness_plugins.login import TokenStore, authorize

    config = inst.load_config()
    credential = authorize(
        profile(args.harness),
        _endpoint(args.endpoint, config),
        store=TokenStore(),
        open_url=lambda url: (print(f"Open to sign in: {url}"), webbrowser.open(url)),
    )
    print(json.dumps({"harness": args.harness, "state": credential.state.value}))
    return 0


def hook_command(args: argparse.Namespace) -> int:
    from arbora.harness_plugins.hook_runner import run
    from arbora.harness_plugins.login import TokenStore

    try:
        prof = profile(args.harness)
        config = inst.load_config()
        output = run(
            Lifecycle(args.point),
            prof,
            sys.stdin.read(),
            base_url=_endpoint(None, config),
            store=TokenStore(),
        )
    except Exception:  # noqa: BLE001 - a hook always exits 0 and never blocks
        output = ""
    if output:
        print(output)
    return 0


def statusline_command(args: argparse.Namespace) -> int:
    """One line: the install and credential state. No network call."""
    from arbora.harness_plugins.login import TokenStore

    try:
        json.loads(sys.stdin.read() or "{}")
    except ValueError:
        pass
    config = inst.load_config()
    record = config.installs.get(args.harness)
    credential = TokenStore().load(args.harness)
    install_state = record.state.value if record else "absent"
    cred_state = credential.state.value if credential else "door sign-in"
    line = f"arbora {install_state} | {cred_state}"
    print(line)
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="arbora plugin",
        description="Generate and install the arbora harness plugins.",
    )
    sub = top.add_subparsers(dest="command", required=True)
    for name in ("bundle", "install", "status"):
        cmd = sub.add_parser(name)
        if name == "status":
            cmd.add_argument("--harness", action="append")
        else:
            cmd.add_argument("--harness", required=True)
        cmd.add_argument("--skills-root", type=Path, action="append")
        if name != "status":
            cmd.add_argument("--endpoint")
            cmd.add_argument("--skill", action="append")
            cmd.add_argument("--out")
        if name == "bundle":
            cmd.add_argument("--print-path", action="store_true")
    remove = sub.add_parser("remove")
    remove.add_argument("--harness", required=True)
    login = sub.add_parser("login")
    login.add_argument("--harness", required=True)
    login.add_argument("--endpoint")
    hook = sub.add_parser("hook")
    hook.add_argument("point", choices=[p.value for p in Lifecycle])
    hook.add_argument("--harness", required=True)
    line = sub.add_parser("statusline")
    line.add_argument("--harness", default="claude-code")
    return top


COMMANDS = {
    "bundle": bundle_command,
    "install": install_command,
    "status": status_command,
    "remove": remove_command,
    "login": login_command,
    "hook": hook_command,
    "statusline": statusline_command,
}


def main(argv: Sequence[str]) -> int:
    args = parser().parse_args(list(argv))
    if args.command == "install":
        args.out = None
    try:
        return COMMANDS[args.command](args)
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return 2


__all__ = ["BundleRefusal", "build", "main", "parser"]
