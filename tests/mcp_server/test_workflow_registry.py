"""The compiled-workflow lookup reads the engine registry and builds its client lazily (ARBORA-HOST-R022.1)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.domain.mcp_server import decide_wiring
from arbora.api.domain.mcp_server.decide_wiring import (
    _workflow_lookup,
    install_decide_consumers,
)
from arbora.api.invoke.pipeline import OperationRefused

DIGEST = "sha256:" + "a" * 64
KNOWN = f"workflow:release@1.2#{DIGEST}"
UNKNOWN = f"workflow:ghost@1#{DIGEST}"
SESSION = SimpleNamespace(graph="graph-a")


class EngineRefused(Exception):
    code = "WORKFLOW_IDENTITY_UNKNOWN"


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """A structural double of the engine ``WorkflowRegistry`` read."""
    from medulla.generated import coordination

    sent: list[dict[str, Any]] = []

    async def send(client: Any, params: dict[str, Any], graph: str) -> Any:
        await asyncio.sleep(0)
        sent.append({"params": params, "graph": graph, "client": client})
        if params["identity"]["workflow_id"] == "ghost":
            raise EngineRefused("unknown")
        return params

    def decode(answer: Any) -> dict[str, Any]:
        return {
            "lifecycle": "active",
            "skills": [
                {
                    "component_id": "skill:a",
                    "kind": "skill",
                    "definition_digest": DIGEST,
                }
            ],
        }

    monkeypatch.setattr(coordination, "send_workflow_registry", send)
    monkeypatch.setattr(coordination, "decode_workflow_registry_read", decode)
    return sent


@pytest.mark.spec("ARBORA-HOST-R022.1")
async def test_first_use_initialization_and_unknown_workflow_refusal(
    registry: list[dict[str, Any]],
) -> None:
    built: list[object] = []

    def factory() -> object:
        built.append(object())
        return built[-1]

    lookup = _workflow_lookup(factory, SESSION)
    assert built == [], "no engine client is built before the first lookup"
    hits = await lookup([KNOWN, "plain task text"])
    assert hits == [
        {
            "kind": "workflow",
            "id": "release",
            "version": "1.2",
            "content_digest": DIGEST,
            "lifecycle": "active",
            "skills": [
                {
                    "component_id": "skill:a",
                    "kind": "skill",
                    "definition_digest": DIGEST,
                }
            ],
        }
    ]
    assert registry[0]["params"] == {
        "op": "read",
        "identity": {
            "workflow_id": "release",
            "version": "1.2",
            "content_digest": DIGEST,
        },
    }
    assert registry[0]["graph"] == "graph-a" and registry[0]["client"] is built[0]
    with pytest.raises(OperationRefused) as refused:
        await lookup([UNKNOWN])
    assert refused.value.code == "NOT_FOUND" and not refused.value.details
    assert len(built) == 1


@pytest.mark.spec("ARBORA-HOST-R022.1")
async def test_concurrent_first_lookups_build_one_client(
    registry: list[dict[str, Any]],
) -> None:
    built: list[object] = []

    def factory() -> object:
        built.append(object())
        return built[-1]

    lookup = _workflow_lookup(factory, SESSION)
    results = await asyncio.gather(*(lookup([KNOWN]) for _ in range(8)))
    assert all(len(r) == 1 for r in results)
    assert len(built) == 1
    assert {id(entry["client"]) for entry in registry} == {id(built[0])}


@pytest.mark.spec("ARBORA-HOST-R022.1")
def test_serving_start_builds_the_lookup_without_reading_the_registry(
    monkeypatch: pytest.MonkeyPatch, registry: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr("arbora.api.ops.plans.engine_task_plan", lambda client: None)
    calls: list[Any] = []
    installed = install_decide_consumers(
        lambda session: calls.append(session) or object(), SESSION, object()
    )
    assert installed["workflow_lookup"] is True
    assert registry == []
    assert len(calls) == 1, "only the existing consumer client; the lookup built none"
    assert decide_wiring._workflow_lookup


@pytest.mark.spec("ARBORA-HOST-R022.1")
def test_a_session_without_a_graph_installs_no_lookup() -> None:
    with pytest.raises(LookupError):
        _workflow_lookup(object, SimpleNamespace(graph=""))
