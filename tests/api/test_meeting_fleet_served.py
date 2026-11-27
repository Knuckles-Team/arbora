"""The production summarizer port dispatches through the governed fleet call (ARBORA-OPS-R065.3)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.meeting_fleet import FleetMeetingSummarizer
from arbora.mcp_server.served_invocation import _with_meeting_summarizer
from tests.api._meeting_fakes import meeting_context


class RecordingGateway:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.effects: list[str] = []
        item = SimpleNamespace(
            id="prov",
            name="summarize_meeting",
            server="notes-mcp",
            annotations={"capability": "meeting_summary"},
        )
        self.catalog = SimpleNamespace(with_capability=self._items(item))

    @staticmethod
    def _items(item: Any) -> Any:
        async def with_capability(caller: Any, capability: str) -> tuple[Any, ...]:
            return (item,) if capability == "meeting_summary" else ()

        return with_capability

    async def effect(self, op: Any, params: Any, caller: Any) -> Any:
        self.effects.append(op.id)
        return SimpleNamespace(effect="read")

    async def call(
        self, caller: Any, server: str, tool: str, arguments: Any, **_: Any
    ) -> Any:
        self.calls.append((server, tool, arguments))
        return self.answer


ADMISSION = {"provider": "prov", "model": "mod", "admission_digest": "d" * 8}
RUN = {"claims": [], "receipt": {"receipt_id": "r1"}}


def _request(gateway: RecordingGateway) -> Any:
    context = meeting_context(None, fleet_gateway=gateway)
    context.service_identity = False
    context.owner = "principal:one"
    context.registry_digest = ""
    return context


async def _run(gateway: RecordingGateway) -> Any:
    return await FleetMeetingSummarizer().run(
        request=_request(gateway),
        tenant="t1",
        context={"context_digest": "c" * 64},
        admission=ADMISSION,
        attempt=1,
    )


@pytest.mark.spec("ARBORA-OPS-R065.3")
async def test_summarizer_port_dispatches_through_fleet_call() -> None:
    gateway = RecordingGateway(RUN)
    assert await _run(gateway) == RUN
    assert gateway.effects == ["fleet.call"]
    ((server, tool, arguments),) = gateway.calls
    assert (server, tool) == ("notes-mcp", "summarize_meeting")
    assert arguments["context"] == {"context_digest": "c" * 64}
    assert arguments["attempt"] == 1


@pytest.mark.spec("ARBORA-OPS-R065.3")
async def test_summarizer_port_refuses_a_connector_error_and_a_malformed_run() -> None:
    for answer in ({"error": "boom"}, {"claims": "x"}, "Error: down"):
        with pytest.raises(OperationRefused) as refused:
            await _run(RecordingGateway(answer))
        assert refused.value.code == "PROVIDER_UNAVAILABLE"


@pytest.mark.spec("ARBORA-OPS-R065.3")
def test_the_served_composition_binds_the_fleet_summarizer() -> None:
    bound = _with_meeting_summarizer({"fleet_gateway": object()})
    assert isinstance(bound["meeting_summarizer"], FleetMeetingSummarizer)
    existing = {"meeting_summarizer": "own"}
    assert _with_meeting_summarizer(existing) is existing
