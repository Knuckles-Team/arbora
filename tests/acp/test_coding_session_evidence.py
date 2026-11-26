"""One trace record per admission and capability request (ARBORA-ACP-R001.7)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import fields

import pytest
from meristem.host_ports import ClientCapabilityRefused

from arbora.acp.adapter import _EffectTransport
from arbora.acp.coding_session import ClientCapability
from arbora.acp.trace import ACP_LOGGER, AcpTraceRecord
from tests.acp.test_coding_session_refusal import _agent, _Forwarded

_CAPS = frozenset({ClientCapability.WRITE_TEXT_FILE, ClientCapability.TERMINAL_CREATE})


@pytest.mark.spec("ARBORA-ACP-R001.7")
def test_record_fields_without_content(caplog: pytest.LogCaptureFixture) -> None:
    assert [f.name for f in fields(AcpTraceRecord)] == [
        "session_id",
        "principal_id",
        "tenant",
        "workspace_root",
        "capability",
        "permission_outcome",
        "refusal_reason",
    ]
    caplog.set_level(logging.INFO, logger=ACP_LOGGER)

    granted = _agent(_CAPS)
    granted._client = _Forwarded("allow_once")  # type: ignore[assignment]
    asyncio.run(
        _EffectTransport(granted, "s").call(
            "fs/write_text_file", {"path": "/workspace/a", "content": "SECRET BODY"}
        )
    )
    denied = _agent(_CAPS)
    denied._client = _Forwarded("reject")  # type: ignore[assignment]
    with pytest.raises(ClientCapabilityRefused):
        asyncio.run(
            _EffectTransport(denied, "s").call(
                "terminal/create", {"command": "echo SECRET OUTPUT"}
            )
        )

    records = [r.acp for r in caplog.records if r.name == ACP_LOGGER]
    outcomes = [(r["capability"], r["permission_outcome"]) for r in records]
    assert ("fs/write_text_file", "granted") in outcomes
    assert ("terminal/create", "denied") in outcomes
    for record in records:
        assert record["principal_id"] == "alice" and record["tenant"] == "tenant"
        assert record["workspace_root"] == "/workspace"
        assert "SECRET" not in repr(record)
