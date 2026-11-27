"""The ``scenario_stack`` fixture, built on the shared served-operation harness.

The fixture adds no second server path: it takes the host application the ``served``
fixture composes, the one engine double behind it, and the HTTP and MCP callers that
reach that application (ARBORA-ACCEPTANCE-R006.5).
"""

from __future__ import annotations

from typing import Any

import pytest

from arbora.testing.scenario_stack import ScenarioStack
from tests.api._served_support import McpAsk, capture_invocation
from tests.test_served_operation_api import _call, _value, served  # noqa: F401


@pytest.fixture
def invocation_capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return capture_invocation(monkeypatch)


@pytest.fixture
def scenario_stack(
    invocation_capture: dict[str, Any],
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> ScenarioStack:
    app, client = served

    def http(op: str, params: dict[str, Any], **headers: str) -> Any:
        return _value(_call(app, op, params, **headers))

    return ScenarioStack(
        application=app,
        engine=client,
        http=http,
        mcp=McpAsk(invocation_capture["invocation"], monkeypatch),
    )
