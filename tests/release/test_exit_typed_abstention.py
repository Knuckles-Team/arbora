"""Exit obligation ``typed_abstention``: an uncovered capability abstains, typed (ARBORA-RELEASE-R003.8)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.api._served_support import RecordingEngine, route_methods
from tests.release._stack import SCOPES, caller, stack  # noqa: F401
from tests.test_served_operation_api import _call, served  # noqa: F401

ABSTENTION = {"abstained": {"reason": "uncovered_capability"}}


@pytest.mark.spec("ARBORA-RELEASE-R003.8")
def test_capability_question_with_no_agent_returns_typed_abstention(
    stack: dict[str, Any],  # noqa: F811
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served
    engine = RecordingEngine()
    route_methods(
        client,
        {"NlQuery": engine.record("NlQuery", ABSTENTION)},
        monkeypatch,
        {"fixture-verified": caller(*SCOPES)},
    )
    reply = _call(
        app, "query.nl", {"text": "does any agent cover orbital mechanics planning"}
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()
    # The engine's abstention reason reaches the caller unchanged, and Arbora
    # invents no agent and adds no abstention text of its own.
    assert "uncovered_capability" in reply.text
    assert "agent_id" not in reply.text
    assert [name for name, *_ in engine.calls] == ["NlQuery"]
    assert body.get("ok") is not False
