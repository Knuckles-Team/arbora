"""Compose one harness-neutral bundle (ARBORA-HARNESS-PLUGINS-R001.2, R003.1).

:func:`compose_bundle` returns a frozen :class:`BundleSpec`: the door MCP entry,
the skills, the agent delegates, the commands and the abstract hook intents. It
never names a harness file; the renderers map it onto a layout. The skill set is
the door skills of ``US-CONTENT-R007`` plus the declared list (owner decision
D4); every other skill is reached through the door's ``find``. The bundle copies
no MCP prompt: the harness lists the served prompts of the door itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from arbora.harness_plugins.profiles import HarnessProfile, Lifecycle

#: The skills every bundle carries (``US-CONTENT-R007``).
DOOR_SKILLS: tuple[str, ...] = ("canopy", "session-handoff")
#: The native public client of ``ARBORA-IDENTITY-R030.4``.
NATIVE_CLIENT_ID = "arbora-native-mcp"
MCP_PATH = "/mcp"
SERVER_NAME = "arbora"
PLUGIN_NAME = "arbora"


class SkillMissing(LookupError):
    """A door or declared skill is not installed under any content root."""


@dataclass(frozen=True)
class DoorEntry:
    url: str
    client_id: str
    callback: str
    callback_port: int


@dataclass(frozen=True)
class SkillRef:
    name: str
    source: Path


@dataclass(frozen=True)
class AgentDef:
    name: str
    description: str
    profile_id: str


@dataclass(frozen=True)
class CommandDef:
    name: str
    description: str
    body: str


@dataclass(frozen=True)
class HookIntent:
    point: Lifecycle
    op: str
    trigger: str | None = None


@dataclass(frozen=True)
class BundleSpec:
    profile_id: str
    door: DoorEntry
    skills: tuple[SkillRef, ...]
    agents: tuple[AgentDef, ...]
    commands: tuple[CommandDef, ...]
    hooks: tuple[HookIntent, ...] = field(default_factory=tuple)


HOOK_INTENTS: tuple[HookIntent, ...] = (
    HookIntent(Lifecycle.FIRST_PROMPT, "workstream.attach"),
    HookIntent(Lifecycle.BEFORE_COMPACTION, "workstream.checkpoint", "compact"),
    HookIntent(Lifecycle.SESSION_END, "workstream.detach"),
)

COMMANDS: tuple[CommandDef, ...] = (
    CommandDef(
        name="handoff",
        description="Record a handoff checkpoint for the next session on this workspace",
        body=(
            "Summarize, in at most 2000 characters, what this session did, what is "
            "left and the open run ids. Then call the arbora `write` tool with "
            '`op="workstream.checkpoint"` and params `harness_session_id` (this '
            'session), `summary`, `run_ids` and `trigger="manual"`. $ARGUMENTS'
        ),
    ),
    CommandDef(
        name="resume",
        description="Read what the last sessions on this workspace left for this one",
        body=(
            'Call the arbora `ask` tool with `op="workstream.get"` and params '
            "`workspace_root` set to the current working directory. Report the last "
            "checkpoint and the other attached sessions, then continue from it. "
            "$ARGUMENTS"
        ),
    ),
)


def door_entry(profile: HarnessProfile, base_url: str) -> DoorEntry:
    """The door MCP entry: the native client and the profile's fixed callback."""
    return DoorEntry(
        url=base_url.rstrip("/") + MCP_PATH,
        client_id=NATIVE_CLIENT_ID,
        callback=profile.callback,
        callback_port=profile.callback_port,
    )


def installed_skill_roots() -> tuple[Path, ...]:
    """The ``skills/`` directory of every installed content package."""
    from arbora.core.providers import content_connectors

    return tuple(
        resolved.dirs.skills
        for resolved in content_connectors()
        if resolved.dirs.skills is not None and Path(resolved.dirs.skills).is_dir()
    )


def find_skills(names: Iterable[str], roots: Sequence[Path]) -> tuple[SkillRef, ...]:
    """Resolve each skill name to the first root that holds ``<name>/SKILL.md``."""
    found = []
    for name in dict.fromkeys(names):
        source = next(
            (Path(r) / name for r in roots if (Path(r) / name / "SKILL.md").is_file()),
            None,
        )
        if source is None:
            raise SkillMissing(name)
        found.append(SkillRef(name=name, source=source))
    return tuple(sorted(found, key=lambda s: s.name))


def compose_bundle(
    profile: HarnessProfile,
    *,
    base_url: str,
    skills: Sequence[str] = (),
    agents: Sequence[AgentDef] = (),
    skill_roots: Sequence[Path] | None = None,
) -> BundleSpec:
    """The frozen bundle for ``profile``; :class:`SkillMissing` names a gap."""
    roots = installed_skill_roots() if skill_roots is None else tuple(skill_roots)
    return BundleSpec(
        profile_id=profile.id,
        door=door_entry(profile, base_url),
        skills=find_skills((*DOOR_SKILLS, *skills), roots),
        agents=tuple(sorted(agents, key=lambda a: a.name)),
        commands=COMMANDS,
        hooks=tuple(i for i in HOOK_INTENTS if profile.event(i.point) is not None),
    )


__all__ = [
    "COMMANDS",
    "DOOR_SKILLS",
    "HOOK_INTENTS",
    "AgentDef",
    "BundleSpec",
    "CommandDef",
    "DoorEntry",
    "HookIntent",
    "SkillMissing",
    "SkillRef",
    "compose_bundle",
    "door_entry",
    "find_skills",
]
