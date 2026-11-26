"""Bundle configuration from six layers that only add (ARBORA-HARNESS-PLUGINS-R013).

The layers apply in this order: company, team, repository, directory, user,
local. A layer may add skills, personas, tools and hooks and may set policy
values. A later layer must not remove an entry or loosen a policy value of an
earlier one. :func:`resolve` checks every layer before it returns, so a refusal
writes nothing. The company and team layers are the served ``config.layers.get``
read; the other four are files the client owns (:func:`file_layers`).
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from arbora.harness_plugins.compose import (
    HOOK_INTENTS,
    BundleSpec,
    find_skills,
)

ORDER: tuple[str, ...] = (
    "company",
    "team",
    "repository",
    "directory",
    "user",
    "local",
)
LIST_KEYS = ("skills", "personas", "tools", "hooks")
#: The closed policy keys and which direction is stricter: a boolean is
#: stricter as ``True``, a count is stricter as it gets lower.
POLICY_KEYS: dict[str, type] = {
    "confirm_writes": bool,
    "max_agents": int,
    "max_skills": int,
    "max_tools": int,
}
_KEYS = frozenset({*LIST_KEYS, "policy"})
BUNDLE_FILE = "bundle.toml"
LOCAL_FILE = "bundle.local.toml"


class LayerRefused(ValueError):
    """A layer removes an entry, loosens a policy value or is malformed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Layer:
    name: str
    settings: Mapping[str, Any]


def _names(layer: Layer, key: str) -> list[str]:
    value = layer.settings.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise LayerRefused("LAYER_MALFORMED", f"{layer.name}.{key}")
    if any(v.startswith(("-", "!")) for v in value):
        raise LayerRefused("LAYER_REMOVES_ENTRY", f"{layer.name}.{key}")
    return value


def _check_keys(layer: Layer) -> None:
    for key in layer.settings:
        if key in ("remove", "removes") or key.startswith(("remove_", "-")):
            raise LayerRefused("LAYER_REMOVES_ENTRY", f"{layer.name}.{key}")
        if key not in _KEYS:
            raise LayerRefused("LAYER_MALFORMED", f"{layer.name}.{key}")


def _loosens(key: str, before: bool | int, after: bool | int) -> bool:
    if POLICY_KEYS[key] is bool:
        return bool(before) and not bool(after)
    return int(after) > int(before)


def _policy(layer: Layer, held: dict[str, bool | int]) -> None:
    table = layer.settings.get("policy", {})
    if not isinstance(table, Mapping):
        raise LayerRefused("LAYER_MALFORMED", f"{layer.name}.policy")
    for key, value in table.items():
        kind = POLICY_KEYS.get(key)
        if (
            kind is None
            or type(value) is not kind
            or (kind is int and isinstance(value, int) and value < 0)
        ):
            raise LayerRefused("LAYER_MALFORMED", f"{layer.name}.policy.{key}")
        setting: bool | int = value if isinstance(value, (bool, int)) else 0
        if key in held and _loosens(key, held[key], setting):
            raise LayerRefused("LAYER_LOOSENS_POLICY", f"{layer.name}.policy.{key}")
        held[key] = setting


def resolve(
    layers: Sequence[Layer], base: BundleSpec, skill_roots: Sequence[Path]
) -> BundleSpec:
    """``base`` plus every layer, in :data:`ORDER`; nothing is dropped."""
    order = {name: i for i, name in enumerate(ORDER)}
    if any(layer.name not in order for layer in layers):
        raise LayerRefused("LAYER_MALFORMED", "layer name")
    added: dict[str, list[str]] = {key: [] for key in LIST_KEYS}
    policy: dict[str, bool | int] = {}
    for layer in sorted(layers, key=lambda layer: order[layer.name]):
        _check_keys(layer)
        for key in LIST_KEYS:
            added[key] += _names(layer, key)
        _policy(layer, policy)
    points = {i.point.value: i for i in HOOK_INTENTS}
    unknown = [h for h in added["hooks"] if h not in points]
    if unknown:
        raise LayerRefused("LAYER_MALFORMED", f"hooks.{unknown[0]}")
    hooks = {i.point: i for i in base.hooks}
    for name in added["hooks"]:
        hooks.setdefault(points[name].point, points[name])
    have = {s.name for s in base.skills}
    extra = [s for s in dict.fromkeys(added["skills"]) if s not in have]
    skills = (*base.skills, *find_skills(extra, skill_roots)) if extra else base.skills
    return replace(
        base,
        skills=tuple(sorted(skills, key=lambda s: s.name)),
        hooks=tuple(hooks[p] for p in hooks),
        personas=tuple(sorted(dict.fromkeys(added["personas"]))),
        tools=tuple(sorted(dict.fromkeys(added["tools"]))),
        policy=tuple(sorted(policy.items())),
    )


def _load(path: Path, name: str) -> Layer | None:
    if not path.is_file():
        return None
    try:
        return Layer(name, tomllib.loads(path.read_text(encoding="utf-8")))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise LayerRefused("LAYER_MALFORMED", name) from None


def repository_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def file_layers(cwd: Path, user_dir: Path) -> list[Layer]:
    """The repository, directory, user and local layers the client owns."""
    root = repository_root(cwd)
    found = [
        _load(root / ".arbora" / BUNDLE_FILE, "repository") if root else None,
        _load(cwd / ".arbora" / BUNDLE_FILE, "directory"),
        _load(user_dir / BUNDLE_FILE, "user"),
        _load(root / ".arbora" / LOCAL_FILE, "local") if root else None,
    ]
    return [layer for layer in found if layer is not None]


def served_layers(answer: Any) -> list[Layer]:
    """The company and team layers of the ``config.layers.get`` answer.

    The engine also lists the caller's user layer; that one is the user's
    served layer and is applied with the user files, at the user position.
    """
    rows = answer.get("layers") if isinstance(answer, Mapping) else None
    layers: list[Layer] = []
    for row in rows or ():
        name = row.get("layer") if isinstance(row, Mapping) else None
        settings = row.get("settings") if isinstance(row, Mapping) else None
        if name in ("company", "team", "user") and isinstance(settings, Mapping):
            layers.append(Layer(name, settings))
    return layers


__all__ = [
    "BUNDLE_FILE",
    "LOCAL_FILE",
    "ORDER",
    "POLICY_KEYS",
    "Layer",
    "LayerRefused",
    "file_layers",
    "resolve",
    "served_layers",
]
