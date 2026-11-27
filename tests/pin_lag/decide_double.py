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
    record = {"record_id": "rec", "outcome": advisory(*taken_options(taken))}
    return {"outcome": "answered", "payload": {"records": [record]}}


def advisory(*options: str) -> dict[str, object]:
    """The engine's advisory outcome: each option id scored in the order the engine seals."""
    return {
        "outcome": "advisory",
        "scores": [
            {"option_id": option, "score": {"value": 1 << 32}} for option in options
        ],
    }


def taken_options(taken: tuple[str, ...]) -> list[str]:
    """The ``taken:<task id>`` option ids of a branch answer."""
    return [f"taken:{task}" for task in taken]
