"""The agent summary widget is dropped, not carried over (ARBORA-OPS-R041).

The widget returned constant data, so no operation, route or module serves it.
"""

from __future__ import annotations

import importlib.util

import pytest

from arbora.api.ops import get_registry


@pytest.mark.spec("ARBORA-OPS-R041")
def test_no_agent_summary_widget_is_served() -> None:
    registry = get_registry()
    served = [
        op.id
        for op in registry
        if op.id in {"agents.summary", "agent.summary", "agents.summary.widget"}
        or op.id.endswith("summary.widget")
    ]
    assert served == []
    assert importlib.util.find_spec("arbora.api.ops.agent_summary") is None
