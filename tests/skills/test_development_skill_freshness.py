"""The repository development skill keeps every original topic (ARBORA-DEPLOY-R010.1)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[2] / "arbora" / "skills"
SKILL = SKILLS / "arbora-repository-development" / "SKILL.md"

#: Each required topic and the heading words that name it.
REQUIRED = {
    "architecture boundaries": "architecture boundaries",
    "build-host rule": "build-host",
    "required gates": "required quality gates",
    "identity setup": "identity setup",
}


def missing_sections(text: str) -> list[str]:
    """The ``SKILL_SECTION_MISSING`` headings: required topics with no heading."""
    headings = [
        match.group(1).strip().lower()
        for match in re.finditer(r"^#{1,6}\s+(.+)$", text, flags=re.MULTILINE)
    ]
    return [
        topic
        for topic, words in REQUIRED.items()
        if not any(words in heading for heading in headings)
    ]


@pytest.mark.spec("ARBORA-DEPLOY-R010.1")
def test_required_sections_are_present() -> None:
    assert missing_sections(SKILL.read_text(encoding="utf-8")) == []
    assert (SKILLS / "arbora-development" / "SKILL.md").is_file()


@pytest.mark.spec("ARBORA-DEPLOY-R010.1")
def test_a_removed_section_is_named() -> None:
    stripped = re.sub(
        r"^## Identity setup.*$",
        "## Other",
        SKILL.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    assert missing_sections(stripped) == ["identity setup"]
