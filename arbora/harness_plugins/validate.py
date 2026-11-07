"""Structural validator per profile layout (ARBORA-HARNESS-PLUGINS-R001.7).

A rendered tree passes when it holds the profile's manifest, one door entry for
the native client, a ``SKILL.md`` per bundled skill and well-formed hooks. The
harness's own validator (where one exists) runs in the release proof; this one
runs offline in the bundle test.
"""

from __future__ import annotations

import json
from pathlib import Path

from arbora.harness_plugins.compose import NATIVE_CLIENT_ID, SERVER_NAME
from arbora.harness_plugins.profiles import HookKind, profile


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not an object")
    return value


def _check_hooks(root: Path, kind: HookKind) -> list[str]:
    problems = []
    hooks = _json(root / "hooks/hooks.json").get("hooks", {})
    for event, groups in hooks.items():
        for group in groups:
            for handler in group.get("hooks", []):
                if handler.get("type") != kind.value:
                    problems.append(f"{event}: hook type {handler.get('type')}")
    return problems


def validate_tree(harness: str, root: Path) -> list[str]:
    """Every structural problem of the tree; empty means valid."""
    prof = profile(harness)
    problems: list[str] = []
    if not (root / prof.manifest).is_file():
        return [f"missing {prof.manifest}"]
    manifest = _json(root / prof.manifest)
    if not manifest.get("name") or not manifest.get("version"):
        problems.append("manifest lacks name or version")
    skills = sorted(p.parent.name for p in (root / "skills").glob("*/SKILL.md"))
    if not skills:
        problems.append("no skills")
    if harness == "opencode":
        bundle = _json(root / "bundle.json")
        if (
            bundle["door"]["clientId"] != NATIVE_CLIENT_ID
            or bundle["door"]["redirectUri"] != prof.callback
        ):
            problems.append("door does not name the native client and callback")
        if sorted(bundle["skills"]) != skills:
            problems.append("bundle skills differ from the skill tree")
        if not (root / "server.ts").is_file():
            problems.append("missing server.ts")
        return problems
    server = _json(root / ".mcp.json")["mcpServers"].get(SERVER_NAME, {})
    if server.get("oauth", {}).get("clientId") != NATIVE_CLIENT_ID:
        problems.append("door entry does not name the native client")
    if "Authorization" in json.dumps(server) or "headers" in server:
        problems.append("door entry carries a header")
    problems += _check_hooks(root, prof.hook_kind)
    return problems


__all__ = ["validate_tree"]
