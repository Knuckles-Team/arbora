"""Work item commit surfaces the owner's typed refusal (ARBORA-OPS-R902)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora import work_items
from arbora.work_items import TransitionRefused

CLAIM = {
    "tenant": "t1",
    "lease_holder_ref": "w1",
    "lease_epoch": 1,
    "fencing_token": 7,
}


class _Refusal(Exception):
    code = "TRANSITION_REFUSED"

    def __init__(self, reason: str) -> None:
        super().__init__("refused")
        self.machine = "WorkItemMachine"
        self.from_state = "ready"
        self.event = "commit"
        self.reason = reason


class _Items:
    def __init__(self, *, answer: str | None = None, error: Exception | None = None):
        self.answer, self.error = answer, error

    def commit_result(self, **_: Any) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        return {"status": self.answer}


def _commit(monkeypatch: pytest.MonkeyPatch, items: _Items, outcome: str) -> str:
    monkeypatch.setattr(work_items, "_work_items", lambda _engine: items)
    monkeypatch.setattr(work_items, "_tenant", lambda tenant=None: "t1")
    return work_items.commit_result(SimpleNamespace(), "wi-1", CLAIM, outcome=outcome)


@pytest.mark.spec("ARBORA-OPS-R902")
def test_illegal_commit_surfaces_the_typed_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(TransitionRefused) as refused:
        _commit(monkeypatch, _Items(error=_Refusal("illegal_transition")), "succeeded")
    assert refused.value.code == "TRANSITION_REFUSED"
    assert (refused.value.machine, refused.value.from_state, refused.value.event) == (
        "WorkItemMachine",
        "ready",
        "commit",
    )


@pytest.mark.spec("ARBORA-OPS-R902")
@pytest.mark.parametrize(
    "outcome", ["succeeded", "failed", "cancelled", "dead_letter"], ids=str
)
def test_commit_outcome_maps_to_the_enum(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    assert _commit(monkeypatch, _Items(answer=outcome), outcome) == "committed"
    assert _commit(monkeypatch, _Items(answer="running"), outcome) == "running"


@pytest.mark.spec("ARBORA-OPS-R902")
def test_terminal_item_commit_is_noop_derived_from_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        _commit(monkeypatch, _Items(error=_Refusal("terminal_state")), "succeeded")
        == "noop"
    )


@pytest.mark.spec("ARBORA-OPS-R902")
def test_other_engine_errors_are_not_mistaken_for_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError):
        _commit(monkeypatch, _Items(error=RuntimeError("boom")), "succeeded")
