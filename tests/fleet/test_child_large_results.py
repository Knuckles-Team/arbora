"""A child result above the MCP client's default SSE event bound (G13).

A 50-record ServiceNow page is one SSE event larger than 1 MiB. The MCP
client's default event bound (1 MiB) refused it as "Server-sent event
exceeded the 1048576 byte limit" and the ingest job failed ``tool_failed``.
Arbora reads a child event up to twice its own delegated value bound.
"""

from __future__ import annotations

import pytest

from arbora.fleet import multiplexer
from tests.fleet.test_relay_session_recovery import _mounted, child_port  # noqa: F401

pytestmark = pytest.mark.spec("ARBORA-OPS-R020.6.8")


async def test_a_two_mib_child_result_reaches_arbora(tmp_path, child_port) -> None:  # noqa: F811
    port, _ = child_port
    mux, runtime = await _mounted(tmp_path, port)
    try:
        result = await runtime.call_tool("big_page", {"size": 2048})
        assert not getattr(result, "is_error", False)
        text = result.content[0].text
        assert len(text.encode()) > 2 * 1024 * 1024
    finally:
        await runtime.aclose()


def test_the_child_event_bound_covers_the_delegated_value_bound() -> None:
    assert (
        multiplexer._MAX_CHILD_SSE_EVENT_BYTES
        >= 2 * multiplexer._MAX_DELEGATED_VALUE_BYTES
    )
