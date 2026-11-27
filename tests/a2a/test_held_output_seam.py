"""The held-output seam with the harness (ARBORA-A2A-R022, MERISTEM-HARNESS-CYCLE-R101).

The harness is a structural double of ``meristem.harness.held_item.held_output_block``:
the SHA-256 of ``config.encode(output)`` (or UTF-8 of the output) beside the received
refs. The gate, the admitter and the authority's terminal read are the real ones.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.a2a.critique_gate import HeldOutput, output_digest, release_output
from arbora.a2a.held_item_admission import HeldItemAdmitter, HeldItemRefusal
from arbora.a2a.held_output import (
    HELD_OUTPUT_FIELDS,
    HeldOutputRefs,
    held_output_config,
    lift_held_output,
    reply_artifact,
    reply_bytes,
    with_held_output,
)
from arbora.a2a.models import A2ATask, A2ATaskStatus
from arbora.run_origin import RunOriginRegistry
from tests.a2a.test_application import _Engine

TASK = "a2a-" + "0" * 64
OUTPUT = "the generated answer"
REFS = HeldOutputRefs(
    item_ref="held:one",
    engine_decision_ref="dec:one",
    checklist=(("q1", "Does the answer cite its source?"),),
    verifier="verifier:one",
    compiler="compiler:one",
    verification_ref="ver:one",
)


@dataclass(frozen=True)
class DoubleHeldOutputConfig:
    """The meristem ``HeldOutputConfig`` shape (pin-lag double)."""

    item_ref: str
    engine_decision_ref: str
    checklist: tuple[tuple[str, str], ...]
    verifier: str | None = None
    compiler: str | None = None
    verification_ref: str | None = None
    encode: Any = None


def harness_block(output: str, config: Any) -> dict[str, Any]:
    """What ``held_output_block`` returns (MERISTEM-HARNESS-CYCLE-R101)."""
    raw = config.encode(output) if config.encode else output.encode("utf-8")
    return {
        "item_ref": config.item_ref,
        "content_digest": hashlib.sha256(raw).hexdigest(),
        "engine_decision_ref": config.engine_decision_ref,
        "checklist": [{"id": i, "question": q} for i, q in config.checklist],
        "verification": {
            "verifier": config.verifier,
            "compiler": config.compiler,
            "verification_ref": config.verification_ref,
        },
    }


@pytest.fixture
def pinned_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import meristem.graph.state as state

    monkeypatch.setattr(
        state, "HeldOutputConfig", DoubleHeldOutputConfig, raising=False
    )


def _terminal_authority(result: dict[str, Any]) -> WorkItemA2AAuthority:
    payload = json.dumps(
        {"type": "graph_complete", "run_id": TASK, "result": {"run_id": TASK, **result}}
    )
    return WorkItemA2AAuthority(
        lambda: SimpleNamespace(query_cypher=lambda q, p: [{"payload": payload}]),
        origins=RunOriginRegistry(),
    )


def _read_task(authority: WorkItemA2AAuthority) -> A2ATask:
    item = {"status": "succeeded", "metadata": {"a2a_run_id": TASK}}
    artifacts, held = authority._persisted_reply(TASK, item=item)
    metadata = {} if held is None else {"heldOutput": held}
    return A2ATask(
        id=TASK,
        context_id="a2a-context-" + "0" * 64,
        status=A2ATaskStatus(state="completed"),
        artifacts=artifacts,
        metadata=metadata,
    )


def _gate(task: A2ATask, engine: _Engine) -> Any:
    async def audit(_entry: Any) -> None:
        return None

    chain = SimpleNamespace(held_items=HeldItemAdmitter(engine, audit))
    return asyncio.run(release_output(task, chain, tenant="tenant-a"))  # type: ignore[arg-type]


@pytest.mark.spec("ARBORA-A2A-R022")
def test_a_real_path_send_with_a_held_output_is_admitted(pinned_config: None) -> None:
    config = held_output_config(REFS, TASK)
    assert isinstance(config, DoubleHeldOutputConfig)
    block = harness_block(OUTPUT, config)
    task = _read_task(
        _terminal_authority(
            {"output": OUTPUT, "status": "completed", "held_output": block}
        )
    )
    # Only the four keys of ``HeldOutput`` are lifted; verification stays outside.
    assert set(task.metadata["heldOutput"]) == set(HELD_OUTPUT_FIELDS)
    assert HeldOutput.model_validate(task.metadata["heldOutput"]).item_ref == "held:one"
    # The harness digested exactly the bytes the gate digests.
    assert block["content_digest"] == output_digest(task)
    engine = _Engine()
    released = _gate(task, engine)
    assert released.metadata["critique"]["outcome"] == "admitted"
    [asked] = engine.asked
    assert (
        asked.item_ref == "held:one" and asked.content_digest == block["content_digest"]
    )


@pytest.mark.spec("ARBORA-A2A-R022")
def test_a_digest_the_harness_computed_over_other_bytes_refuses(
    pinned_config: None,
) -> None:
    # The host did not pass ``encode``: the harness digests the bare UTF-8 text.
    config = replace(held_output_config(REFS, TASK), encode=None)
    block = harness_block(OUTPUT, config)
    task = _read_task(
        _terminal_authority(
            {"output": OUTPUT, "status": "completed", "held_output": block}
        )
    )
    engine = _Engine()
    with pytest.raises(HeldItemRefusal) as caught:
        _gate(task, engine)
    assert caught.value.reason == "DIGEST_MISMATCH" and engine.asked == []


@pytest.mark.spec("ARBORA-A2A-R022")
def test_a_run_without_a_block_stays_held() -> None:
    task = _read_task(_terminal_authority({"output": OUTPUT, "status": "completed"}))
    assert "heldOutput" not in task.metadata
    with pytest.raises(HeldItemRefusal) as caught:
        _gate(task, _Engine())
    assert caught.value.reason == "POLICY_EVIDENCE_MISSING"
    assert lift_held_output({"held_output": {"item_ref": "x"}}) is None
    assert lift_held_output(None) is None


@pytest.mark.spec("ARBORA-A2A-R022")
def test_encode_is_the_exact_bytes_the_gate_digests(pinned_config: None) -> None:
    config = held_output_config(REFS, TASK)
    task = A2ATask(
        id=TASK,
        context_id="a2a-context-" + "0" * 64,
        status=A2ATaskStatus(state="completed"),
        artifacts=[reply_artifact(TASK, OUTPUT)],
    )
    assert config.encode(OUTPUT) == reply_bytes(TASK, OUTPUT)
    assert hashlib.sha256(config.encode(OUTPUT)).hexdigest() == output_digest(task)
    assert held_output_config(None, TASK) is None


@dataclass(frozen=True)
class _Harness:
    held_output: Any = None
    retries: int = 1


@dataclass(frozen=True)
class _Plan:
    harness: Any = field(default_factory=_Harness)


@dataclass(frozen=True)
class _OldHarness:
    retries: int = 1


@pytest.mark.spec("ARBORA-A2A-R022")
def test_the_run_plan_carries_the_config_only_where_the_pin_has_the_field(
    pinned_config: None,
) -> None:
    config = held_output_config(REFS, TASK)
    assert with_held_output(_Plan(), config).harness.held_output is config
    plan = _Plan()
    assert with_held_output(plan, None) is plan
    # PIN-LAG: a pinned harness without the field is left as it is.
    old = _Plan(harness=_OldHarness())
    assert with_held_output(old, config) is old


@pytest.mark.spec("ARBORA-A2A-R022")
def test_the_run_decision_sets_held_output_for_a_run_that_has_refs(
    monkeypatch: pytest.MonkeyPatch, pinned_config: None
) -> None:
    import meristem.runtime as runtime

    from arbora import agent_runs

    monkeypatch.setattr(runtime, "default_run_plan", lambda request, cfg: _Plan())
    owner = SimpleNamespace(held_output=REFS, task_id=TASK, hosted_invocation=None)
    monkeypatch.setattr(agent_runs, "current_native_run", lambda request: owner)
    plan = agent_runs.decide_run(SimpleNamespace(run_id=TASK))
    assert plan.harness.held_output.item_ref == "held:one"
    owner.held_output = None
    assert (
        agent_runs.decide_run(SimpleNamespace(run_id=TASK)).harness.held_output is None
    )
