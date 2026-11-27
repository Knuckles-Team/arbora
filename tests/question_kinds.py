"""A structural double of an engine release that names ``Decide`` question kinds."""

from __future__ import annotations

import typing

import pytest


def declare_question_kinds(monkeypatch: pytest.MonkeyPatch, *kinds: str) -> None:
    """Make the pinned contract name each ``kind`` (snake_case) as a ``QuestionKind``."""
    from medulla.generated import models

    literal = typing.Literal[kinds]  # type: ignore[valid-type]
    monkeypatch.setattr(models, "QuestionKind", models.QuestionKind | literal)
