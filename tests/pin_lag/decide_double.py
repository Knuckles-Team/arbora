"""A structural double of the engine ``main`` contract that lists the control questions."""

from __future__ import annotations

from typing import Literal

import pytest


def serve_control_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pinned engine's ``QuestionKind`` plus ``branch_select`` and ``decision_table_eval``."""
    from medulla.generated import models

    monkeypatch.setattr(
        models,
        "QuestionKind",
        Literal["route"]
        | Literal["task_plan"]
        | Literal["branch_select"]
        | Literal["decision_table_eval"],
        raising=False,
    )


def answered_branch(*taken: str) -> dict[str, object]:
    """The ``RunCall`` envelope of an answered ``BranchSelect`` that took ``taken``."""
    record = {"record_id": "rec", "answer": {"value": {"taken_task_ids": list(taken)}}}
    return {"outcome": "answered", "payload": {"records": [record]}}
