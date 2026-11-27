"""The closed harness profile table (ARBORA-HARNESS-PLUGINS-R001.1).

One row per coding harness that arbora renders a plugin for. Every harness fact
the composer, the renderers, install and the hook runner need lives here: the
manifest layout, the fixed loopback OAuth callback, the hook kind and the event
map from abstract lifecycle points to the harness's own event names. A case that
differs between harnesses is a field of this table, never a branch on a name.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from urllib.parse import urlsplit

#: The native public client of ``ARBORA-IDENTITY-R030.4``.
NATIVE_CLIENT_ID = "arbora-native-mcp"
#: The confidential client of a hosted agent (``ARBORA-HARNESS-PLUGINS-R010.2``).
HOSTED_CLIENT_ID = "arbora-hosted-mcp"


class HookKind(StrEnum):
    #: The harness calls an MCP tool of the door directly from a hook.
    MCP_TOOL = "mcp_tool"
    #: The harness runs ``arbora plugin hook`` with the login credential.
    COMMAND = "command"
    #: The agent runs on a vendor cloud and has no hook (``hosted``).
    NONE = "none"


class Lifecycle(StrEnum):
    FIRST_PROMPT = "first_prompt"
    BEFORE_COMPACTION = "before_compaction"
    SESSION_END = "session_end"
    #: One skill use ended; the hook reports it as one telemetry log record.
    SKILL_END = "skill_end"


class Part(StrEnum):
    SKILLS = "skills"
    COMMANDS = "commands"
    AGENTS = "agents"
    HOOKS = "hooks"
    MCP = "mcp"
    STATUSLINE = "statusline"


@dataclass(frozen=True)
class HarnessProfile:
    id: str
    #: The manifest file, relative to the bundle root; ``None`` for a profile
    #: that writes no plugin manifest (``hosted``).
    manifest: str | None
    #: The fixed loopback redirect URI of the native client for this harness;
    #: empty for a profile with no loopback callback (``hosted``).
    callback: str
    hook_kind: HookKind
    #: ``(lifecycle point, harness event)``; a point with no event is absent.
    events: tuple[tuple[Lifecycle, str], ...]
    parts: frozenset[Part]
    #: Harness config files, relative to the home directory, that may hold a
    #: hand-written door entry (``ARBORA-HARNESS-PLUGINS-R002.3``).
    door_configs: tuple[str, ...]
    #: Harness skill directories, relative to home, that may hold door skill links.
    skill_dirs: tuple[str, ...]
    #: Hook timeout unit of the harness hook file.
    timeout_unit: str = "seconds"
    #: The OAuth client the bundle names (the native public client by default).
    client_id: str = NATIVE_CLIENT_ID
    #: The oldest harness release the bundle is built for (ARBORA-HOST-R008.6).
    minimum_version: str = "0.0.0"
    #: The parser of the harness configuration file: ``json`` or ``toml``.
    parser: str = "json"
    #: The key holding the MCP server entries in that configuration file.
    mcp_entry_format: str = "mcpServers"

    def event(self, point: Lifecycle) -> str | None:
        return dict(self.events).get(point)

    @property
    def callback_port(self) -> int | None:
        """The loopback callback port, or ``None`` for a profile with no callback."""
        if not self.callback:
            return None
        port = urlsplit(self.callback).port
        assert port is not None
        return port


_ALL = frozenset(Part)
_CC_EVENTS = (
    (Lifecycle.FIRST_PROMPT, "UserPromptSubmit"),
    (Lifecycle.BEFORE_COMPACTION, "PreCompact"),
    (Lifecycle.SESSION_END, "SessionEnd"),
    (Lifecycle.SKILL_END, "PostToolUse"),
)

PROFILES: tuple[HarnessProfile, ...] = (
    HarnessProfile(
        id="claude-code",
        manifest=".claude-plugin/plugin.json",
        callback="http://localhost:33418/callback",
        hook_kind=HookKind.MCP_TOOL,
        events=_CC_EVENTS,
        parts=_ALL,
        door_configs=(".claude.json", ".claude/settings.json"),
        skill_dirs=(".claude/skills",),
        minimum_version="1.0.0",
        parser="json",
        mcp_entry_format="mcpServers",
    ),
    HarnessProfile(
        id="opencode",
        manifest="package.json",
        callback="http://127.0.0.1:19876/mcp/oauth/callback",
        hook_kind=HookKind.COMMAND,
        events=(
            (Lifecycle.FIRST_PROMPT, "session.context"),
            (Lifecycle.BEFORE_COMPACTION, "session.compaction"),
            (Lifecycle.SESSION_END, "session.deleted"),
            (Lifecycle.SKILL_END, "tool.execute.after"),
        ),
        parts=frozenset({Part.SKILLS, Part.COMMANDS, Part.HOOKS, Part.MCP}),
        door_configs=(
            ".config/opencode/opencode.json",
            ".config/opencode/opencode.jsonc",
        ),
        skill_dirs=(".config/opencode/skills", ".config/opencode/skill"),
        timeout_unit="milliseconds",
        minimum_version="0.1.0",
        parser="json",
        mcp_entry_format="mcp",
    ),
    HarnessProfile(
        id="codex",
        manifest=".codex-plugin/plugin.json",
        callback="http://127.0.0.1:33419/callback",
        hook_kind=HookKind.COMMAND,
        events=(
            (Lifecycle.FIRST_PROMPT, "UserPromptSubmit"),
            (Lifecycle.BEFORE_COMPACTION, "PreCompact"),
        ),
        parts=frozenset({Part.SKILLS, Part.HOOKS, Part.MCP}),
        door_configs=(".codex/config.toml",),
        skill_dirs=(".codex/skills", ".agents/skills"),
        minimum_version="0.1.0",
        parser="toml",
        mcp_entry_format="mcp_servers",
    ),
    HarnessProfile(
        id="grok",
        manifest=".grok-plugin/plugin.json",
        callback="http://127.0.0.1:33420/callback",
        hook_kind=HookKind.COMMAND,
        events=_CC_EVENTS,
        parts=frozenset(
            {Part.SKILLS, Part.COMMANDS, Part.AGENTS, Part.HOOKS, Part.MCP}
        ),
        door_configs=(".grok/settings.json", ".grok/mcp.json"),
        skill_dirs=(".grok/skills",),
        minimum_version="0.1.0",
        parser="json",
        mcp_entry_format="mcpServers",
    ),
    HarnessProfile(
        id="hosted",
        manifest=None,
        callback="",
        hook_kind=HookKind.NONE,
        events=(),
        parts=frozenset({Part.SKILLS, Part.MCP}),
        door_configs=(),
        skill_dirs=(),
        client_id=HOSTED_CLIENT_ID,
        minimum_version="0.0.0",
        parser="json",
        mcp_entry_format="mcpServers",
    ),
)

_BY_ID = MappingProxyType({p.id: p for p in PROFILES})

#: The harness target and version matrix, derived from the one profile table.
TARGET_MATRIX: tuple[dict[str, str], ...] = tuple(
    {
        "harness": p.id,
        "minimum_version": p.minimum_version,
        "parser": p.parser,
        "mcp_entry_format": p.mcp_entry_format,
    }
    for p in PROFILES
)


def profile(name: str) -> HarnessProfile:
    """The profile named ``name``; ``KeyError`` for a name outside the table."""
    return _BY_ID[name]


def native_redirect_uris() -> tuple[str, ...]:
    """The loopback callback of each profile, in table order (ARBORA-IDENTITY-R030.4.7).

    The one list of native redirect URIs: the identity contract reads it, and a profile
    with no callback (``hosted``) adds none.
    """
    return tuple(p.callback for p in PROFILES if p.callback)


def profile_ids() -> tuple[str, ...]:
    return tuple(_BY_ID)


__all__ = [
    "HOSTED_CLIENT_ID",
    "NATIVE_CLIENT_ID",
    "PROFILES",
    "native_redirect_uris",
    "HarnessProfile",
    "HookKind",
    "Lifecycle",
    "Part",
    "profile",
    "profile_ids",
]
