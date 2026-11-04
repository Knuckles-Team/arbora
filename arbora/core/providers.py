"""Installed-content discovery over the connector SDK (``ARBORA-HOST-R001.10``).

Every package that carries content declares itself once, as the single entry
point of the ``scion_sdk.connector`` group: the skills packages and
any content-only connector are found by
``scion_sdk.connector.installed_connectors()``. Arbora's own package
is the host, not a served connector, so its content directories come from the
SDK's own ``content_dirs("arbora")`` convention. Content is found by convention
under each package (``skills/``, ``prompts/``, ``ontology/``). There are no
provider entry-point groups to read.

Skill identity is global: two installed packages that declare the same skill
name are reported by name and the first connector in fleet-name order keeps it.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from scion_sdk.connector import (
    Connector,
    ResolvedConnector,
    content_dirs,
    installed_connectors,
)

from arbora._version import __version__

logger = logging.getLogger(__name__)

_FRONTMATTER_NAME = re.compile(r"(?m)^name:\s*[\"']?([^\r\n#\"']+?)[\"']?\s*$")
_MAX_SKILL_BYTES = 1_048_576


OWN_PACKAGE = "arbora"


def own_content() -> ResolvedConnector:
    """Arbora's own package content, found by the SDK's directory convention."""
    return ResolvedConnector(
        name=OWN_PACKAGE,
        package=OWN_PACKAGE,
        distribution=OWN_PACKAGE,
        version=__version__,
        connector=Connector(),
        dirs=content_dirs(OWN_PACKAGE),
    )


def content_connectors() -> list[ResolvedConnector]:
    """Arbora's own content and every installed package that declares content."""
    declared = [r for r in installed_connectors() if r.name != OWN_PACKAGE]
    return sorted([own_content(), *declared], key=lambda r: r.name)


def _skill_dirs(root: Path) -> list[Path]:
    return sorted(path.parent for path in root.glob("*/SKILL.md") if path.is_file())


def _frontmatter_identity(text: str) -> str | None:
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    match = _FRONTMATTER_NAME.search(text[3:end] if end >= 0 else text[:4096])
    if match is None:
        return None
    identity = match.group(1).strip()
    if identity and len(identity) <= 256 and all(ord(ch) >= 32 for ch in identity):
        return identity
    return None


def skill_identity(skill_root: Path) -> str:
    """The skill's frontmatter ``name``, else its directory name."""
    path = skill_root / "SKILL.md"
    if path.stat().st_size > _MAX_SKILL_BYTES:
        raise ValueError(f"skill {skill_root.name!r} exceeds its size bound")
    text = path.read_text(encoding="utf-8")
    return _frontmatter_identity(text) or skill_root.name


def resolve_skill_provider_dirs(
    connectors: list[ResolvedConnector] | None = None,
) -> list[tuple[str, Path]]:
    """One ``(connector name, skill directory)`` per globally unique skill.

    A duplicate identity is logged by name with both connectors and skipped;
    it never drops the other skills.
    """
    selected: dict[str, tuple[str, Path]] = {}
    for connector in sorted(
        connectors if connectors is not None else content_connectors(),
        key=lambda item: item.name.casefold(),
    ):
        for skill_root in _skill_dirs(connector.dirs.skills):
            identity = skill_identity(skill_root).casefold()
            if identity in selected:
                owner, existing = selected[identity]
                logger.error(
                    "Skipping duplicate skill %r from %r (%s): already provided "
                    "by %r (%s)",
                    identity,
                    connector.name,
                    skill_root,
                    owner,
                    existing,
                )
                continue
            selected[identity] = (connector.name, skill_root)
    return [selected[identity] for identity in sorted(selected)]


__all__ = [
    "content_connectors",
    "own_content",
    "resolve_skill_provider_dirs",
    "skill_identity",
]
