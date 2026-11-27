"""The host publishes the process control-step schemas once per tenant (ARBORA-OPS-R898.5).

The engine does not publish ``decide.schema.process.branch_select`` or
``decide.schema.process.decision_table_eval`` itself (coordinator decision). Arbora publishes
the engine's exported bodies at start-up, as it does the catalog ranking schemas, and the
control-step decision port pins each digest the engine returns. Until the client exports the
bodies the control steps refuse; after the re-pin they stop refusing.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from arbora.api.domain.agent_host import host_port_adapters as adapters
from arbora.api.task_plan_schema import (
    PROCESS_QUESTION_IDS,
    process_schema_pins,
    publish_at_startup,
    rank_pin,
)
from tests.pin_lag.decide_double import answered_branch
from tests.pin_lag.test_host_ports_pin_lag import main_shape  # noqa: F401
from tests.question_kinds import declare_question_kinds

TENANT = "tenant:one"
BODY = {
    "schema_version": 1,
    "features": [
        {
            "name": "arm_count",
            "kind": {"feature": "number", "key": "process.arm_count"},
            "missing": {"missing": "impute", "value": {"scale": "q32", "value": 0}},
        }
    ],
}


class Stat:
    """The engine body codec: the digest is the hash of the compact body."""

    @staticmethod
    def encode_body(body: Any) -> tuple[str, Any]:
        import hashlib
        import json

        raw = json.dumps(body, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(raw).hexdigest(), {}


def _export(*, corrupt: bool = False, extra: bool = False) -> dict[str, Any]:
    digest, _ = Stat.encode_body(BODY)
    names = (*PROCESS_QUESTION_IDS, *(("process.other",) if extra else ()))
    return {
        q: {
            "component_id": f"decide.schema.{q}",
            "kind": "feature_schema",
            "schema_version": 1,
            "content_digest": "sha256:bad" if corrupt else digest,
            "idempotency_key": f"eg:{q}",
            "body": BODY,
        }
        for q in names
    }


@pytest.fixture
def engine_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = types.ModuleType("fake_process_exports")
    monkeypatch.setitem(sys.modules, "fake_process_exports", module)
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "9.9.9")
    return module


def _pins(module: Any) -> dict[str, Any]:
    return process_schema_pins(modules=("fake_process_exports",), stat=Stat())


@pytest.mark.spec("ARBORA-OPS-R898.5")
def test_publish_once_per_tenant_then_the_control_step_stops_refusing(
    engine_module: Any,
    main_shape: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declare_question_kinds(monkeypatch, "branch_select", "decision_table_eval")
    engine_module.PROCESS_CONTROL_FEATURE_SCHEMAS = _export(extra=True)
    pins = _pins(engine_module)
    assert sorted(pins) == sorted(
        PROCESS_QUESTION_IDS
    )  # an unknown question is not bound
    assert rank_pin(pins, TENANT, "process.branch_select") is None  # not yet published

    # Before the publish the control step sends nothing and refuses.
    sent: list[Any] = []

    async def call(op: str, params: Any, ref: str) -> Any:
        sent.append(params)
        return answered_branch("t2")

    def port() -> Any:
        return adapters.decision_port(
            call,
            tenant_of=lambda _: TENANT,
            schema_of=lambda t, q: rank_pin(pins, t, q),
            graph_of=lambda _: "graph:one",
        )

    with pytest.raises(RuntimeError) as refused:
        asyncio.run(port()("BranchSelect", {}, "b1"))
    assert refused.value.code == "decide_request_incomplete"  # type: ignore[attr-defined]
    assert sent == []

    published: list[tuple[str, str]] = []

    async def publish(tenant: str, pin: Any) -> str:
        published.append((tenant, pin["component_id"]))
        return "sha256:" + "e" * 64

    for pin in pins.values():
        done = asyncio.run(publish_at_startup(pin, publish, (TENANT, TENANT)))
        assert done == {TENANT: True}
    assert sorted(published) == [
        (TENANT, "decide.schema.process.branch_select"),
        (TENANT, "decide.schema.process.decision_table_eval"),
    ]  # once per tenant per question
    verdict = asyncio.run(port()("BranchSelect", {"branch_task_id": "b1"}, "b1"))
    assert verdict.taken_task_ids == ("t2",)
    [params] = sent
    pinned = params["request"]["feature_schema"]
    assert pinned["definition_digest"] == "sha256:" + "e" * 64
    assert pinned["component_id"] == "decide.schema.process.branch_select"


@pytest.mark.spec("ARBORA-OPS-R898.5")
def test_an_engine_without_the_export_publishes_nothing(engine_module: Any) -> None:
    assert _pins(engine_module) == {}


@pytest.mark.spec("ARBORA-OPS-R898.5")
def test_an_export_that_does_not_match_its_digest_is_refused(
    engine_module: Any,
) -> None:
    engine_module.PROCESS_CONTROL_FEATURE_SCHEMAS = _export(corrupt=True)
    assert _pins(engine_module) == {}
