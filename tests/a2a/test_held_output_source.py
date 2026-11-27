"""The held-output refs source over engine methods (ARBORA-A2A-R022.1).

The engine is a structural double of the node write (``CreateNodeIfAbsent``), the node
read (``query_cypher``) and the ``held_item_admission`` ``Decide`` record. The source, the
authority's terminal read, the admitter and the ``POST /a2a`` route are the real ones.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arbora.a2a.application import create_a2a_application
from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.a2a.engine_deciders import HELD_ITEM_KIND
from arbora.a2a.held_item_admission import HeldItemAdmitter
from arbora.a2a.held_output_source import (
    HELD_REFS_LABEL,
    HeldOutputSource,
    bind_held_output,
    held_item_id,
)
from arbora.a2a.models import A2AArtifact
from arbora.a2a.service import A2AService
from arbora.run_origin import RunOriginRegistry
from tests.a2a.test_application import Authenticator, Authority, Router, _send

TENANT = "tenant:one"
TASK = "a2a-" + "0" * 64
OUTPUT = "the generated answer"
CHECKLIST = [{"id": "q1", "question": "Does the answer cite its source?"}]


class Engine:
    """Node write and read of the held nodes, plus the terminal run event."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.writes = 0

    def create_node(self, node_id: str, label: str, properties: dict[str, Any]) -> bool:
        self.writes += 1
        created = node_id not in self.nodes
        self.nodes.setdefault(node_id, {**properties, "type": label})
        return created

    def query_cypher(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "RunEvent" in query:
            event = {
                "type": "graph_complete",
                "run_id": TASK,
                "result": {"run_id": TASK, "output": OUTPUT, "status": "completed"},
            }
            return [{"payload": json.dumps(event)}]
        return [
            {
                "item_ref": node["item_ref"],
                "content_digest": node["content_digest"],
                "engine_decision_ref": node["engine_decision_ref"],
                "checklist_json": node["checklist_json"],
            }
            for node in self.nodes.values()
            if node.get("type") == params["label"]
            and node["task_id"] == params["task_id"]
        ]


class Decide:
    """The engine ``Decide`` seam: a critique verdict bound to the stored digest."""

    def __init__(self, engine: Engine, verdict: str = "admitted") -> None:
        self.engine, self.verdict = engine, verdict
        self.asked: list[tuple[str, dict[str, str], str]] = []

    async def __call__(
        self, kind: str, params: dict[str, str], tenant: str
    ) -> dict[str, Any]:
        self.asked.append((kind, dict(params), tenant))
        stored = self.engine.nodes[params["item_ref"]]
        bound = stored["content_digest"] == params["content_digest"]
        value: dict[str, Any] = {
            "outcome": self.verdict if bound else "abstained",
            "verdict_ref": f"decision:{len(self.asked)}",
            "checklist": CHECKLIST,
        }
        if not bound:
            value["reason"] = "DIGEST_MISMATCH"
        return {"answer": {"type": "held_item_admission", "value": value}}


class HeldDecider:
    """The admitter's decider over the same ``Decide`` seam.

    PIN-LAG: the pinned engine lacks ``FamilyAnswerHeldItemAdmission``, so
    ``EngineHeldItemDecider`` is unavailable; the typed verdict is read from the record.
    """

    def __init__(self, decide: Decide) -> None:
        self._decide = decide

    async def decide(self, request: Any, *, tenant: str) -> dict[str, Any]:
        record = await self._decide(
            HELD_ITEM_KIND,
            {
                "item_kind": request.item_kind,
                "item_ref": request.item_ref,
                "content_digest": request.content_digest,
            },
            tenant,
        )
        value = record["answer"]["value"]
        return {k: v for k, v in value.items() if k != "checklist"}


async def _produce(engine: Engine, decide: Decide) -> dict[str, Any] | None:
    return await HeldOutputSource(decide).produce(
        engine, tenant=TENANT, task_id=TASK, text=OUTPUT
    )


def _served(engine: Engine, decide: Decide) -> TestClient:
    """The task a real terminal read yields, sent through the real ``/a2a`` route."""
    reader = WorkItemA2AAuthority(lambda: engine, origins=RunOriginRegistry())
    reader.held_output_source = HeldOutputSource(decide)
    artifacts, held = reader._persisted_reply(
        TASK, item={"status": "succeeded", "metadata": {"a2a_run_id": TASK}}
    )
    authority = Authority()
    authority.task = authority.task.model_copy(
        update={
            "artifacts": [A2AArtifact.model_validate(a) for a in artifacts or []],
            "metadata": {} if held is None else {"heldOutput": held},
        }
    )

    async def audit(_entry: Any) -> None:
        return None

    chain = SimpleNamespace(held_items=HeldItemAdmitter(HeldDecider(decide), audit))
    return TestClient(
        create_a2a_application(
            service=A2AService(authority=authority, router=Router()),
            authenticator=Authenticator(),
            approvals=chain,  # type: ignore[arg-type]
        )
    )


@pytest.mark.spec("ARBORA-A2A-R022.1")
@pytest.mark.asyncio
async def test_the_source_writes_the_held_node_and_the_decision_refs() -> None:
    engine = Engine()
    decide = Decide(engine)
    block = await _produce(engine, decide)
    assert block is not None
    digest = block["content_digest"]
    # The node id is the item_ref and the digest binds the exact reply bytes.
    assert block["item_ref"] == held_item_id(TENANT, TASK, digest)
    node = engine.nodes[block["item_ref"]]
    assert node["held_item_kind"] == "run_output_proposal"
    assert node["content_digest"] == digest and node["lifecycle"] == "active"
    # The engine decision ref is the sealing record, and its checklist is the checklist.
    assert (
        block["engine_decision_ref"] == "decision:1" and block["checklist"] == CHECKLIST
    )
    assert decide.asked[0][0] == HELD_ITEM_KIND
    assert engine.nodes[f"{block['item_ref']}:refs"]["type"] == HELD_REFS_LABEL
    # A replay writes nothing new: both nodes are create-if-absent under stable ids.
    before = set(engine.nodes)
    again = await _produce(engine, Decide(engine))
    assert set(engine.nodes) == before
    assert again is not None and again["item_ref"] == block["item_ref"]


@pytest.mark.spec("ARBORA-A2A-R022.1")
@pytest.mark.asyncio
async def test_arbora_writes_no_critique_field_and_reads_the_checklist() -> None:
    engine = Engine()
    block = await _produce(engine, Decide(engine))
    assert block is not None and block["checklist"] == CHECKLIST
    written = {key for node in engine.nodes.values() for key in node}
    assert not {"critique_ref", "critique_digest", "critique_verdict"} & written

    # An answer with no checklist yields no block: the checklist is the engine's.
    class NoChecklist(Decide):
        async def __call__(self, kind: str, params: dict[str, str], tenant: str):  # type: ignore[no-untyped-def]
            record = await super().__call__(kind, params, tenant)
            del record["answer"]["value"]["checklist"]
            return record

    bare = Engine()
    assert await _produce(bare, NoChecklist(bare)) is None
    assert any(n.get("type") == "HeldItem" for n in bare.nodes.values())


@pytest.mark.spec("ARBORA-A2A-R022.1")
@pytest.mark.asyncio
async def test_a_send_with_an_artifact_is_admitted_end_to_end() -> None:
    engine = Engine()
    decide = Decide(engine)
    assert await _produce(engine, decide) is not None
    body = _send(_served(engine, decide)).json()
    assert body["result"]["artifacts"][0]["parts"][0]["text"] == OUTPUT
    assert body["result"]["metadata"]["critique"]["outcome"] == "admitted"
    # The gate asked about the node the source wrote, bound to the output digest.
    kind, params, tenant = decide.asked[-1]
    assert kind == HELD_ITEM_KIND and params["item_kind"] == "run_output_proposal"
    assert params["item_ref"] == body["result"]["metadata"]["heldOutput"]["item_ref"]


@pytest.mark.spec("ARBORA-A2A-R022.1")
@pytest.mark.asyncio
async def test_a_critique_deny_keeps_the_output_held() -> None:
    engine = Engine()
    assert await _produce(engine, Decide(engine, "rejected")) is not None
    body = _send(_served(engine, Decide(engine, "rejected"))).json()
    assert "result" not in body, "a rejected output must send nothing"
    assert body["error"]["code"] == -32011


@pytest.mark.spec("ARBORA-A2A-R022.1")
@pytest.mark.asyncio
async def test_without_a_checklist_or_an_answer_no_refs_exist_and_the_output_stays_held() -> (
    None
):
    engine = Engine()

    async def bare(kind: str, params: dict[str, str], tenant: str) -> dict[str, Any]:
        return {
            "answer": {
                "type": "held_item_admission",
                "value": {"outcome": "admitted", "verdict_ref": "decision:1"},
            }
        }

    assert await _produce(engine, bare) is None  # type: ignore[arg-type]

    async def unavailable(*_args: Any) -> Any:
        raise RuntimeError("engine unreachable")

    assert await _produce(engine, unavailable) is None  # type: ignore[arg-type]
    body = _send(_served(engine, Decide(engine))).json()
    assert "result" not in body and body["error"]["data"]["reason"] == (
        "POLICY_EVIDENCE_MISSING"
    )


@pytest.mark.spec("ARBORA-A2A-R022.1")
def test_binding_needs_the_whole_decision_port() -> None:
    authority = WorkItemA2AAuthority(lambda: None, origins=RunOriginRegistry())
    assert not bind_held_output(authority, SimpleNamespace())
    assert authority.held_output_source is None
    assert bind_held_output(
        authority,
        SimpleNamespace(
            engine_send=lambda *_a: None,
            decision_graph="graph:one",
            decision_feature_schema=lambda tenant: {},
            decision_principal=lambda: "service:host",
        ),
    )
    assert isinstance(authority.held_output_source, HeldOutputSource)
