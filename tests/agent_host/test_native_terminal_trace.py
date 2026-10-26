"""Native acceptance diagnostics retain only bounded operation metadata."""

import asyncio
import json

import pytest

from tests.agent_host.native_terminal_trace import NativeTransportTrace


@pytest.mark.asyncio
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
    error_type: type[BaseException], category: str
) -> None:
    trace = NativeTransportTrace()
    error = error_type("credential=private-value /private/path session=private-id")

    async def denied() -> None:
        raise error

    with pytest.raises(error_type) as raised:
        await trace.observe("read", denied)
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
