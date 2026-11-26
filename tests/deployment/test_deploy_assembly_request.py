"""The assembly request names the template and components; the graph returns as is."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.deployment.assembly import assemble_deployment

pytestmark = pytest.mark.spec("ARBORA-DEPLOY-R017.2")


def test_task_and_capabilities_sent_graph_returned_as_is() -> None:
    sent: list[dict[str, Any]] = []
    answer = {
        "graph": {"shape": {"nodes": [{"node_id": "n1"}]}},
        "record": {"record_id": "r1"},
        "schema_version": 1,
    }

    async def send(body: dict[str, Any]) -> Any:
        sent.append(body)
        return answer

    result = asyncio.run(
        assemble_deployment(
            send, tenant="tenant:one", target="compose", components=["c1", "c2"]
        )
    )
    assert result is answer
    assert sent == [
        {
            "request": {
                "tenant_id": "tenant:one",
                "template_key": "deployment:compose",
                "requirements": {"capabilities": ["c1", "c2"]},
            }
        }
    ]


def test_an_abstention_passes_through_naming_the_engine_reason() -> None:
    abstained = {
        "graph": None,
        "record": {"outcome": {"abstained": {"reason": "uncovered_capability"}}},
    }

    async def send(_body: dict[str, Any]) -> Any:
        return abstained

    result = asyncio.run(
        assemble_deployment(send, tenant="t", target="helm", components=[])
    )
    assert result is abstained


def test_a_malformed_engine_answer_is_refused() -> None:
    async def send(_body: dict[str, Any]) -> Any:
        return [1]

    with pytest.raises(OperationRefused) as refused:
        asyncio.run(assemble_deployment(send, tenant="t", target="x", components=[]))
    assert refused.value.code == "UNAVAILABLE"
