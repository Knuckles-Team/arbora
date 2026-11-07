"""Render one deterministic plugin tree per harness profile.

``ARBORA-HARNESS-PLUGINS-R001.3``, ``R001.6``, ``R005``, ``R006``, ``R008`` and
``R009``. :func:`render` writes the files of one profile layout, checks the tree
(no secret, no path outside the tree) and returns its digest: SHA-256 over the
sorted relative paths and file bytes, the manifest excluded. The digest is the
bundle version; the manifest carries it. Every key and list is sorted, so two
renders of one :class:`BundleSpec` give one digest. Nothing here is committed:
the tree is written on the developer's machine at the point of use.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Callable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

from arbora.harness_plugins.compose import (
    PLUGIN_NAME,
    SERVER_NAME,
    BundleSpec,
    HookIntent,
)
from arbora.harness_plugins.profiles import HarnessProfile, HookKind, Lifecycle, profile

#: The hook runner of ``ARBORA-HARNESS-PLUGINS-R007``.
RUNNER = "arbora plugin hook"
HOOK_TIMEOUT_SECONDS = 10
DESCRIPTION = "The arbora door: the ecosystem's operations, skills and agents, and a workstream shared across harnesses."

_SECRET = re.compile(
    r"(?i)(-----BEGIN [A-Z ]*PRIVATE KEY|\bgh[pousr]_[A-Za-z0-9]{20,}|\bsk-[A-Za-z0-9]{20,}"
    r"|\bxox[abp]-[A-Za-z0-9-]{10,}|\"(client_secret|password|access_token|refresh_token)\"\s*:"
    r"|\bBearer\s+[A-Za-z0-9._-]{16,})"
)
_ABS_PATH = re.compile(
    r"(?<![\w:/.])(/(?:home|Users|var|tmp|root|mnt|srv|opt|etc)/[^\s\"'`)]+)"
)


class TreeRefused(ValueError):
    """The tree holds a secret or a path outside itself; nothing is published."""

    def __init__(self, code: str, path: str) -> None:
        super().__init__(f"{code}: {path}")
        self.code = code
        self.path = path


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def tree_digest(root: Path, *, exclude: tuple[str, ...] = ()) -> str:
    """SHA-256 over sorted relative paths and file bytes."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel in exclude:
            continue
        digest.update(rel.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def check_tree(root: Path, *, allowed_urls: tuple[str, ...] = ()) -> None:
    """Refuse a secret, a symlink or an absolute path outside the tree."""
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise TreeRefused("PATH_ESCAPES_TREE", rel)
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for url in allowed_urls:
            text = text.replace(url, "")
        if _SECRET.search(text):
            raise TreeRefused("SECRET_IN_BUNDLE", rel)
        match = _ABS_PATH.search(text)
        if match and not Path(match.group(1)).resolve().is_relative_to(root.resolve()):
            raise TreeRefused("PATH_ESCAPES_TREE", rel)


def _copy_skills(spec: BundleSpec, root: Path, into: str) -> None:
    for skill in spec.skills:
        source = skill.source.resolve()
        for path in sorted(skill.source.rglob("*")):
            if not path.resolve().is_relative_to(source):
                raise TreeRefused("PATH_ESCAPES_TREE", f"{skill.name}/{path.name}")
            if path.is_file():
                target = root / into / skill.name / path.relative_to(skill.source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)


def _hook_input(intent: HookIntent, profile_id: str) -> dict[str, Any]:
    """The ``write`` tool arguments of one hook, with harness input substitution."""
    params: dict[str, Any] = {"harness_session_id": "${session_id}"}
    if intent.point is Lifecycle.FIRST_PROMPT:
        params |= {"harness": profile_id, "workspace_root": "${cwd}"}
    if intent.point is Lifecycle.BEFORE_COMPACTION:
        params |= {
            "trigger": intent.trigger or "compact",
            "summary": "Context compacted (${trigger}); run /arbora:handoff for a full handoff.",
        }
    return {"op": intent.op, "params": params, "execute": True}


def hook_entries(
    intents: tuple[HookIntent, ...], prof: HarnessProfile
) -> dict[str, Any]:
    """Harness event -> matcher groups, for both hook kinds."""
    events: dict[str, Any] = {}
    for intent in intents:
        event = prof.event(intent.point)
        if event is None:
            continue
        if prof.hook_kind is HookKind.MCP_TOOL:
            handler: dict[str, Any] = {
                "type": "mcp_tool",
                "server": f"plugin:{PLUGIN_NAME}:{SERVER_NAME}",
                "tool": "write",
                "input": _hook_input(intent, prof.id),
                "timeout": HOOK_TIMEOUT_SECONDS,
            }
        else:
            handler = {
                "type": "command",
                "command": f"{RUNNER} {intent.point.value} --harness {prof.id}",
                "timeout": HOOK_TIMEOUT_SECONDS,
            }
        events[event] = [{"hooks": [handler]}]
    return {"hooks": dict(sorted(events.items()))}


def _frontmatter(fields: Mapping[str, str], body: str) -> str:
    head = "".join(f"{k}: {json.dumps(v)}\n" for k, v in sorted(fields.items()))
    return f"---\n{head}---\n\n{body.strip()}\n"


def _agent_body(name: str, profile_id: str) -> str:
    return (
        f"You delegate to the registered arbora agent `{profile_id}`. Call the arbora "
        f'`act` tool with `op="agents.run"` and params `{{"profile": "{profile_id}", '
        '"input": <the task>}`, preview first, then resubmit with the returned '
        "`plan_ref` and `execute=true`. Report the run result as it is."
    )


def _shared_parts(spec: BundleSpec, root: Path, prof: HarnessProfile) -> None:
    """Skills, agents, commands and ``.mcp.json`` in the shared plugin layout."""
    _copy_skills(spec, root, "skills")
    for agent in spec.agents:
        _write(
            root,
            f"agents/{agent.name}.md",
            _frontmatter(
                {"name": agent.name, "description": agent.description},
                _agent_body(agent.name, agent.profile_id),
            ),
        )
    for command in spec.commands:
        _write(
            root,
            f"commands/{command.name}.md",
            _frontmatter({"description": command.description}, command.body),
        )
    _write(
        root, ".mcp.json", _dump({"mcpServers": {SERVER_NAME: _mcp_server(spec, prof)}})
    )
    _write(root, "hooks/hooks.json", _dump(hook_entries(spec.hooks, prof)))


def _mcp_server(spec: BundleSpec, prof: HarnessProfile) -> dict[str, Any]:
    oauth: dict[str, Any] = {
        "clientId": spec.door.client_id,
        "callbackPort": spec.door.callback_port,
    }
    return {"type": "http", "url": spec.door.url, "oauth": oauth}


def _manifest(version: str) -> dict[str, Any]:
    return {
        "name": PLUGIN_NAME,
        "version": version,
        "description": DESCRIPTION,
        "author": {"name": "arbora"},
    }


def render_claude_code(spec: BundleSpec, root: Path) -> None:
    """``.claude-plugin/plugin.json`` plus the shared parts (R005.1)."""
    _shared_parts(spec, root, profile("claude-code"))


def render_grok(spec: BundleSpec, root: Path) -> None:
    """The shared layout and a one-entry local marketplace file (R009)."""
    _shared_parts(spec, root, profile("grok"))
    _write(
        root,
        ".grok-plugin/marketplace.json",
        _dump(
            {"name": "arbora-local", "plugins": [{"name": PLUGIN_NAME, "source": "./"}]}
        ),
    )


def render_codex(spec: BundleSpec, root: Path) -> None:
    """Skills, a streamable HTTP door entry and command hooks (R008)."""
    prof = profile("codex")
    _copy_skills(spec, root, "skills")
    server = {
        "type": "http",
        "url": spec.door.url,
        "oauth": {"clientId": spec.door.client_id},
    }
    _write(root, ".mcp.json", _dump({"mcpServers": {SERVER_NAME: server}}))
    _write(root, "hooks/hooks.json", _dump(hook_entries(spec.hooks, prof)))


OPENCODE_TEMPLATE = "templates/opencode"


def _template(name: str) -> str:
    package = resources.files("arbora.harness_plugins")
    return package.joinpath(f"{OPENCODE_TEMPLATE}/{name}").read_text(encoding="utf-8")


def render_opencode(spec: BundleSpec, root: Path) -> None:
    """One server plugin directory: the template module and its bundle data (R006.1)."""
    prof = profile("opencode")
    _copy_skills(spec, root, "skills")
    _write(root, "server.ts", _template("server.ts"))
    bundle = {
        "harness": prof.id,
        "door": {
            "name": SERVER_NAME,
            "url": spec.door.url,
            "clientId": spec.door.client_id,
            "redirectUri": spec.door.callback,
        },
        "skills": [s.name for s in spec.skills],
        "commands": [
            {"name": c.name, "description": c.description, "template": c.body}
            for c in spec.commands
        ],
        "hooks": {
            event: intent.point.value
            for intent in spec.hooks
            if (event := prof.event(intent.point)) is not None
        },
        "runner": RUNNER,
    }
    _write(root, "bundle.json", _dump(bundle))


RENDERERS: dict[str, Callable[[BundleSpec, Path], None]] = {
    "claude-code": render_claude_code,
    "opencode": render_opencode,
    "codex": render_codex,
    "grok": render_grok,
}


def render(spec: BundleSpec, out_dir: Path) -> str:
    """Write the profile tree into an empty ``out_dir`` and return its digest."""
    prof = profile(spec.profile_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(out_dir.iterdir()):
        shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
    RENDERERS[prof.id](spec, out_dir)
    check_tree(out_dir, allowed_urls=(spec.door.url,))
    digest = tree_digest(out_dir, exclude=(prof.manifest,))
    from arbora.version import __version__

    version = f"{__version__}+{digest[:12]}"
    if prof.id == "opencode":
        manifest: dict[str, Any] = {
            "name": "arbora-opencode-plugin",
            "version": version,
            "private": True,
            "type": "module",
            "main": "server.ts",
            "description": DESCRIPTION,
        }
    else:
        manifest = _manifest(version)
    _write(out_dir, prof.manifest, _dump(manifest))
    return digest


__all__ = [
    "RENDERERS",
    "TreeRefused",
    "check_tree",
    "hook_entries",
    "render",
    "render_claude_code",
    "render_codex",
    "render_grok",
    "render_opencode",
    "tree_digest",
]
