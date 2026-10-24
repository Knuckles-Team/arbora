"""The task-plan schema pin: committed body, verified, published per tenant (R003.4.4).

The engine export is a fixture module with the shape epistemic-graph 2.27.5
exports (``TASK_PLAN_FEATURE_SCHEMA``: body, ``schema_version``, content
digest). The body digest and the publish params use the installed engine's
own helpers and request model, so only the export itself is a fixture.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("epistemic_graph.decision_stat")
from epistemic_graph import decision_stat as ds  # noqa: E402

from arbora.api import task_plan_schema as pins_module  # noqa: E402
from arbora.api.invoke.pipeline import OperationRefused  # noqa: E402
from arbora.api.ops import plans  # noqa: E402

FIXTURE_MODULE = "arbora_test_fixture_eg_task_plan"
DEFINITION = "sha256:" + "d" * 64


def _body(name: str = "prompt") -> dict[str, Any]:
    return ds.feature_schema_body([ds.feature(name, ds.text_bm25("summary", "query"))])


def _export(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or _body()
    return {
        "body": body,
        "schema_version": body["schema_version"],
        "content_digest": ds.encode_body(body)[0],
    }


@pytest.fixture
def engine_export(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = ModuleType(FIXTURE_MODULE)
    module.TASK_PLAN_FEATURE_SCHEMA = _export()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, FIXTURE_MODULE, module)
    return module


_GENERATE = pins_module.generate_pin


def _pin(engine_version: str = "2.27.5") -> dict[str, Any]:
    return _GENERATE(modules=(FIXTURE_MODULE,), engine_version=engine_version)


# spec: ARBORA-RELEASE-R003.4.4
def test_gen_pins_the_exported_body_and_its_digest(engine_export: ModuleType) -> None:
    pin = _pin()
    assert pin["available"] is True
    assert pin["export"] == f"{FIXTURE_MODULE}.TASK_PLAN_FEATURE_SCHEMA"
    assert pin["body"] == _body()
    assert pin["content_digest"] == ds.encode_body(_body())[0]
    assert pins_module.verify(pin, _pin(engine_version="2.27.6")) is None


# spec: ARBORA-RELEASE-R003.4.4
def test_an_export_that_does_not_hash_to_its_digest_is_refused(
    engine_export: ModuleType,
) -> None:
    engine_export.TASK_PLAN_FEATURE_SCHEMA = {  # type: ignore[attr-defined]
        **_export(),
        "content_digest": "sha256:" + "0" * 64,
    }
    with pytest.raises(ValueError, match="does not match its digest"):
        _pin()


# spec: ARBORA-RELEASE-R003.4.4
def test_digest_mismatch_or_missing_export_keeps_the_op_unavailable(
    engine_export: ModuleType,
) -> None:
    committed = _pin()
    engine_export.TASK_PLAN_FEATURE_SCHEMA = _export(_body("other"))  # type: ignore[attr-defined]
    reason = pins_module.verify(committed, _pin())
    assert reason is not None and "content_digest" in reason
    missing = pins_module.generate_pin(
        modules=("no_such_eg_module",), engine_version="x"
    )
    assert missing["available"] is False
    assert "does not export" in str(pins_module.verify(committed, missing))
    assert "no engine export" in str(pins_module.verify(missing, committed))


# spec: ARBORA-RELEASE-R003.4.4
def test_startup_logs_the_reason_and_binds_nothing_on_mismatch(
    engine_export: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "task_plan_schema.json"
    path.write_bytes(pins_module.pin_bytes(_pin()))
    monkeypatch.setattr(pins_module, "generate_pin", lambda: _pin())
    assert pins_module.startup_pins(path) is not None
    engine_export.TASK_PLAN_FEATURE_SCHEMA = _export(_body("other"))  # type: ignore[attr-defined]
    assert pins_module.startup_pins(path) is None
    assert "content_digest" in caplog.text and "UNAVAILABLE" in caplog.text


# spec: ARBORA-RELEASE-R003.4.4
def test_publishes_once_per_tenant_and_uses_the_returned_definition(
    engine_export: ModuleType,
) -> None:
    calls: list[str] = []

    async def publish(tenant: str, pin: Any) -> str:
        calls.append(tenant)
        await asyncio.sleep(0)
        return DEFINITION + tenant

    pins = pins_module.TaskPlanSchemaPins(_pin())

    async def scenario() -> list[dict[str, Any]]:
        first = await asyncio.gather(
            *(pins.for_tenant("tenant-a", publish) for _ in range(5))
        )
        return [*first, await pins.for_tenant("tenant-b", publish)]

    results = asyncio.run(scenario())
    assert calls == ["tenant-a", "tenant-b"]
    assert results[0] == {
        "component_id": pins_module.COMPONENT_ID,
        "kind": "feature_schema",
        "definition_digest": DEFINITION + "tenant-a",
    }
    assert results[-1]["definition_digest"] == DEFINITION + "tenant-b"


def _context(tenant: str) -> dict[str, Any]:
    return {
        "request_id": 1,
        "principal": "service:arbora",
        "caller_principal": "service:arbora",
        "attempt_nonce": "01" * 32,
        "tenant_id": tenant,
        "actor_scope": "service:arbora",
        "purpose_id": pins_module.PURPOSE_ID,
        "policy_revision": "policy:test",
        "policy_digest": "sha256:" + "a" * 64,
        "policy_decision_id": "",
        "idempotency_key": "k",
        "expected_revision": None,
        "trace_id": None,
        "created_at_ms": 1,
    }


class _Engine:
    """The two AgentComponent calls the publisher sends, recorded."""

    def __init__(self, current: Any = None) -> None:
        self.current = current
        self.sent: list[tuple[dict[str, Any], str | None]] = []

    async def _send(
        self,
        method: str,
        params: Any,
        graph: Any = None,
        *,
        idempotency_key: Any = None,
    ) -> Any:
        assert method == "AgentComponent"
        if params["op"]["op"] == "current":
            return self.current
        self.sent.append((params, idempotency_key))
        return {"component": {"definition_digest": DEFINITION}}


# spec: ARBORA-RELEASE-R003.4.4
def test_engine_publish_sends_a_valid_request_and_reuses_a_current_entry(
    engine_export: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("epistemic_graph.generated.storage")
    pin = _pin()
    engine = _Engine()

    async def client_for(_tenant: str) -> Any:
        return engine

    publish = pins_module.engine_publish(client_for, _context)
    assert asyncio.run(publish("tenant-a", pin)) == DEFINITION
    ((params, key),) = engine.sent
    component = params["op"]["request"]["component"]
    assert component["content_digest"] == pin["content_digest"]
    assert (
        component["tenant_id"] == "tenant-a" and component["kind"] == "feature_schema"
    )
    assert (
        ds.decode_body(component["content_digest"], component["attributes"])
        == (pin["body"])
    )
    assert key == pins_module.idempotency_key(pin, "tenant-a")

    entry = SimpleNamespace(
        content_digest=pin["content_digest"], definition_digest="sha256:" + "c" * 64
    )
    reuse = _Engine(current=entry)

    async def reuse_client(_tenant: str) -> Any:
        return reuse

    from epistemic_graph.generated import storage

    async def current(client: Any, request: Any, graph: Any = None, **_: Any) -> Any:
        return client.current

    monkeypatch.setattr(storage, "send_agent_component_current", current)
    digest = asyncio.run(pins_module.engine_publish(reuse_client, _context)("t", pin))
    assert digest == "sha256:" + "c" * 64 and reuse.sent == []


# spec: ARBORA-RELEASE-R003.4.4
def test_plan_run_refuses_unavailable_without_a_verified_pin() -> None:
    context = SimpleNamespace(services={}, caller=SimpleNamespace(tenant="t"))
    with pytest.raises(OperationRefused) as refused:
        asyncio.run(plans._feature_schema(context, "t"))
    assert refused.value.code == "UNAVAILABLE"

    async def failing(_tenant: str, _pin: Any) -> str:
        raise RuntimeError("engine down")

    context.services = {
        "task_plan_schema_pins": pins_module.TaskPlanSchemaPins(
            {"component_id": "c", "kind": "feature_schema"}
        ),
        "task_plan_schema_publish": failing,
    }
    with pytest.raises(OperationRefused) as refused:
        asyncio.run(plans._feature_schema(context, "t"))
    assert refused.value.code == "UNAVAILABLE"


# spec: ARBORA-RELEASE-R003.4.4
def test_committed_pin_matches_the_installed_engine() -> None:
    committed = pins_module.load_pin()
    installed = pins_module.generate_pin()
    assert pins_module.drift(committed, installed) == []
    assert committed["available"] == installed["available"]


@pytest.mark.spec("ARBORA-RELEASE-R003.4.4")
def test_schema_warmup_without_loop_reports_deferred_cause(caplog):
    from arbora.mcp_server.served_invocation import _warm_task_plan_schema

    _warm_task_plan_schema(object(), object(), "tenant")
    assert "task-plan schema warmup deferred to first use" in caplog.text
    assert "no running event loop" in caplog.text
