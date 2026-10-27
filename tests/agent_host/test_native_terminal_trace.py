"""Native acceptance diagnostics retain only bounded operation metadata."""

import asyncio
import json
from typing import Literal

import httpx
import pytest

from tests.agent_host.native_terminal import NativeTerminal
from tests.agent_host.native_terminal_trace import (
    NativeTransportTrace,
    is_log_unavailable_response,
)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"detail":"log_unavailable"}', True),
        (b'{"detail":"private-value"}', False),
        (b"not-json private-value", False),
        (b"x" * 4097, False),
    ],
)
def test_health_error_classification_is_bounded(body: bytes, expected: bool) -> None:
    assert is_log_unavailable_response(httpx.Response(503, content=body)) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["read", "session", "emit"])
@pytest.mark.parametrize(
    ("error_type", "category"),
    [
        (PermissionError, "permission_denied"),
        (TimeoutError, "timeout"),
        (asyncio.CancelledError, "cancelled"),
        (RuntimeError, "error"),
    ],
)
async def test_exception_metadata_never_contains_exception_text(
    error_type: type[BaseException],
    category: str,
    stage: Literal["read", "session", "emit"],
) -> None:
    trace = NativeTransportTrace()
    error = error_type("credential=private-value /private/path session=private-id")

    async def denied() -> None:
        raise error

    with pytest.raises(error_type) as raised:
        await trace.observe(stage, denied)
    assert raised.value is error
    events = json.loads(trace.summary())
    assert events[-1]["status"] == category
    assert events[0]["status"] == "started"
    assert "private" not in trace.summary()
    assert all(set(event) == {"stage", "status", "elapsed_ms"} for event in events)
    assert all(event["elapsed_ms"] >= 0 for event in events)


@pytest.mark.asyncio
async def test_result_is_unchanged_and_trace_is_bounded() -> None:
    trace = NativeTransportTrace()
    result = {"credential": "private-value", "body": "private-body"}

    async def completed() -> dict[str, str]:
        return result

    for _ in range(40):
        assert await trace.observe("bind", completed) is result
    events = json.loads(trace.summary())
    assert len(events) == 64
    assert events[-1]["status"] == "completed"
    assert "private" not in trace.summary()


def test_log_open_waits_for_fixture_task_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = NativeTerminal.__new__(NativeTerminal)
    steps: list[str | bytes] = []
    monkeypatch.setattr(terminal, "wait_for", lambda text: steps.append(text))
    monkeypatch.setattr(terminal, "send", lambda keys: steps.append(keys))
    terminal.open_logs()
    assert steps == [
        "l: host logs",
        "Arbora task read refused or unavailable",
        b"l",
    ]
