#!/usr/bin/env python
"""Skills served from THIS process's own installed packages.

Every installed package that declares ``agent_connector_sdk.connector`` (arbora
itself, the skills packages and any content-only connector) is found by
``arbora.core.providers.resolve_skill_provider_dirs()`` and served as
``skill://{name}/SKILL.md`` by ``arbora.content.register_installed_content``.
This module projects the SAME resolution into the SAME catalog entry shape the
fleet harvest produces (``name``/``uri``/``description``/``instructions``), so
``arbora.fleet.multiplexer`` folds it into one probed "server" entry and ranks
it with fleet tools and fleet-harvested skills alike, and onboarding imports it
the same way (``ARBORA-FLEET-R025``).

Nothing here re-scans installed packages: ``resolve_skill_provider_dirs()``
deduplicates by skill identity across packages and is the one resolution the
wire resources use too.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path

from arbora import skills as _own_skills_package

logger = logging.getLogger(__name__)

# The pseudo-"server" name this module's entries are filed under in the
# multiplexer's probe cache -- never a real mountable fleet child, so it never
# appears in ``load_catalog()`` and is never dialed as a connection target.
LOCAL_SKILLS_SERVER = "arbora-skills"

# Mirrors ``arbora.fleet.multiplexer._MAX_SKILL_BODY_BYTES`` (the bound a
# fleet-harvested skill body is held to) -- kept as an independent constant
# rather than an import, so this module stays free of any dependency on the
# multiplexer and the orphan-module wiring gate sees one clean import
# direction (multiplexer -> this module, never the reverse).
_MAX_SKILL_BODY_BYTES = 512 * 1024

_FRONTMATTER_FIELD_RE = re.compile(r"^(name|description):\s*(.*)$")
_BLOCK_SCALAR_MARKERS = (">-", ">", "|-", "|")


def _bounded_skill_name(name: str) -> bool:
    """Whether a resolved skill's name is a safe, boundable catalog identifier.

    Same boundary ``multiplexer._bounded_catalog_name`` applies to a
    fleet-harvested skill name, reimplemented locally (see the module
    docstring) rather than imported, since the multiplexer imports THIS
    module and not the other way around.
    """
    return (
        isinstance(name, str)
        and 1 <= len(name.encode("utf-8")) <= 256
        and all(ord(character) >= 32 for character in name)
    )


def _frontmatter_block_value(lines: list[str], start: int) -> str:
    """Collect an indented YAML block-scalar continuation (``>-``/``|-``)."""
    collected: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            continue
        if line[:1] in (" ", "\t"):
            collected.append(line.strip())
            continue
        break
    return " ".join(collected)


def _frontmatter_fields(text: str) -> tuple[str | None, str | None]:
    """Best-effort ``name``/``description`` out of a ``SKILL.md``'s frontmatter.

    A lightweight line scan, not a YAML parser -- the same tradeoff
    ``arbora.core.providers._frontmatter_identity`` already makes for
    ``name`` alone; this additionally resolves a folded/literal block
    ``description`` by joining its indented continuation lines.
    """
    if not text.startswith("---"):
        return None, None
    end = text.find("\n---", 3)
    block = text[3:end] if end >= 0 else text[3:4096]
    lines = block.splitlines()
    fields: dict[str, str] = {}
    for index, line in enumerate(lines):
        match = _FRONTMATTER_FIELD_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if value in _BLOCK_SCALAR_MARKERS:
            value = _frontmatter_block_value(lines, index + 1)
        else:
            value = value.strip('"').strip("'")
        fields.setdefault(key, value)
    return fields.get("name"), fields.get("description")


def _read_skill_body(path: Path) -> str:
    """Read one ``SKILL.md`` body, bounded the same as a harvested skill's."""
    data = path.read_bytes()
    if len(data) > _MAX_SKILL_BODY_BYTES:
        raise ValueError(f"skill body at {path} exceeds {_MAX_SKILL_BODY_BYTES} bytes")
    return data.decode("utf-8")


def _local_skill_entry(provider: str, skill_root: Path) -> dict[str, str]:
    """One catalog-shaped entry for a resolved ``(provider, skill_root)`` pair.

    Raises ``OSError``/``UnicodeDecodeError``/``ValueError`` for anything
    unreadable or malformed; the caller reports and skips exactly that one
    skill rather than letting it sink the whole catalog build.
    """
    body = _read_skill_body(skill_root / "SKILL.md")
    fm_name, fm_description = _frontmatter_fields(body)
    name = fm_name or skill_root.name
    if not _bounded_skill_name(name):
        raise ValueError(f"skill name {name!r} from provider {provider!r} is invalid")
    return {
        "name": name,
        "uri": f"skill://{name}/SKILL.md",
        "description": fm_description or "",
        "instructions": body,
    }


def _default_provider_dirs() -> list[tuple[str, Path]]:
    """Lazy-imported default resolver, so importing this module never requires
    ``agent_utilities`` to be installed (only calling it, unpatched, does)."""
    from arbora.core.providers import resolve_skill_provider_dirs

    return resolve_skill_provider_dirs()


def build_local_skill_catalog(
    *, provider_dirs: Callable[[], list[tuple[str, Path]]] | None = None
) -> tuple[list[dict[str, str]], list[str]]:
    """Every current skill resolved from a provider installed in THIS process.

    ``provider_dirs`` defaults to :func:`_default_provider_dirs` (arbora's
    ``resolve_skill_provider_dirs``); a test injects a fixture
    resolver instead, so this stays hermetic without agent-utilities installed.

    Returns ``(entries, problems)``: ``entries`` in the exact shape
    ``arbora.fleet.multiplexer._bounded_skill_catalog`` produces for a
    harvested skill (``name``, ``uri``, ``description``, ``instructions``),
    and ``problems`` naming each individually skipped, unreadable, or
    malformed skill (never a bulk/silent drop).
    """
    resolve = provider_dirs or _default_provider_dirs
    entries: list[dict[str, str]] = []
    problems: list[str] = []
    for provider, skill_root in resolve():
        try:
            entries.append(_local_skill_entry(provider, skill_root))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            problems.append(
                f"{provider}:{skill_root.name}: {type(exc).__name__}: {exc}"
            )
    return entries, problems


def core_pack_names() -> list[str]:
    """The always-offered skill names from ``arbora/skills/core-pack.txt``.

    One name per line; blank lines and ``#``-prefixed comments are ignored.
    """
    import importlib.resources as _resources

    try:
        text = (_resources.files(_own_skills_package) / "core-pack.txt").read_text(
            "utf-8"
        )
    except OSError:
        return []
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def missing_core_pack_names(
    entries: list[dict[str, str]], names: list[str] | None = None
) -> list[str]:
    """Core-pack names no resolved local skill provides.

    Reported loudly by the caller (a startup log), never fatal: a renamed or
    not-yet-installed provider must not stop Arbora from serving every OTHER
    skill it did resolve.
    """
    available = {entry["name"] for entry in entries}
    wanted = names if names is not None else core_pack_names()
    return [name for name in wanted if name not in available]
