"""The task-plan schema pin: committed body, verified, published per tenant (R003.4.4).

The engine export is a fixture module with the shape epistemic-graph 2.27.5
exports (``TASK_PLAN_FEATURE_SCHEMA``: body, ``schema_version``, content
digest). The body digest and the publish params use the installed engine's
own helpers and request model, so only the export itself is a fixture.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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
from arbora.epistemic import ClientContext, EpistemicClientPool  # noqa: E402

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


def test_released_export_survives_pin_file_roundtrip() -> None:
    pin = _GENERATE()
    restored = json.loads(pins_module.pin_bytes(pin))
    assert ds.encode_body(restored["body"])[0] == pin["content_digest"]
    assert ds.canonical_body_bytes(restored["body"]) == ds.canonical_body_bytes(
        pin["body"]
    )


def test_outer_pin_metadata_order_does_not_change_serialized_bytes(
    engine_export: ModuleType,
) -> None:
    pin = _pin()
    assert pins_module.pin_bytes(pin) == pins_module.pin_bytes(
        dict(reversed(list(pin.items())))
    )


@pytest.mark.parametrize("change", ["reorder", "corrupt"])
def test_startup_rejects_body_drift_with_unchanged_declared_digest(
    engine_export: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    change: str,
) -> None:
    pin = _pin()
    damaged = json.loads(pins_module.pin_bytes(pin))
    if change == "reorder":
        damaged["body"] = dict(reversed(list(pin["body"].items())))
    else:
        damaged["body"]["schema_version"] += 1
    assert damaged["content_digest"] == pin["content_digest"]
    path = tmp_path / "task_plan_schema.json"
    path.write_text(json.dumps(damaged))
    monkeypatch.setattr(pins_module, "generate_pin", lambda: pin)
    assert pins_module.startup_pins(path) is None
    assert "body" in caplog.text and "digest" in caplog.text
    assert "UNAVAILABLE" in caplog.text


@pytest.mark.parametrize("change", ["missing", "non-object", "non-json", "non-finite"])
def test_verify_returns_a_reason_for_malformed_committed_body(
    engine_export: ModuleType, change: str
) -> None:
    installed = _pin()
    damaged = dict(installed)
    if change == "missing":
        del damaged["body"]
    else:
        damaged["body"] = {
            "non-object": [],
            "non-json": {"feature": object()},
            "non-finite": {"feature": float("nan")},
        }[change]
    reason = pins_module.verify(damaged, installed)
    assert isinstance(reason, str) and "body" in reason


def test_verify_does_not_mask_an_unrelated_engine_import_failure(
    engine_export: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = _pin()

    def unavailable() -> Any:
        raise ImportError("engine codec unavailable")

    monkeypatch.setattr(pins_module, "_stat", unavailable)
    with pytest.raises(ImportError, match="engine codec unavailable"):
        pins_module.verify(pin, pin)


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
            *(pins.publish_for("tenant-a", publish) for _ in range(5))
        )
        return [*first, await pins.publish_for("tenant-b", publish)]

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


async def _publication_context(tenant: str, _pin: Any) -> dict[str, Any]:
    return _context(tenant)


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


class _ScopedEngine(_Engine):
    """Raw transport that refuses generated sends without client_scope."""

    authenticated = True

    def __init__(self, *, fail_publish: bool = False) -> None:
        super().__init__()
        self.context: dict[str, Any] | None = None
        self.seen_contexts: list[dict[str, Any]] = []
        self.fail_publish = fail_publish

    @contextlib.contextmanager
    def use_verified_context(self, context: dict[str, Any]):
        previous = self.context
        self.context = context
        try:
            yield self
        finally:
            self.context = previous

    async def _send(
        self,
        method: str,
        params: Any,
        graph: Any = None,
        *,
        idempotency_key: Any = None,
    ) -> Any:
        if self.context is None:
            from arbora.api.contracts.session import SessionRequiredError

            raise SessionRequiredError("a verified client context is required")
        self.seen_contexts.append(dict(self.context))
        if params["op"]["op"] == "publish" and self.fail_publish:
            raise RuntimeError("schema publication transport failed")
        return await super()._send(
            method, params, graph, idempotency_key=idempotency_key
        )


def _client_context(tenant: str, principal: str) -> ClientContext:
    return ClientContext(
        principal=principal,
        tenant=tenant,
        audience="epistemic-graph",
        agent_id=principal,
        roles=(),
        scopes=("agent:pack-control",),
        policy_version="policy:test",
    )


def test_startup_publisher_preserves_released_export_and_returned_definition(
    tmp_path: Path,
) -> None:
    pin = _GENERATE()
    path = tmp_path / "task_plan_schema.json"
    path.write_bytes(pins_module.pin_bytes(pin))
    pins = pins_module.startup_pins(path)
    assert pins is not None
    engine = _Engine()

    async def client_for(_tenant: str) -> Any:
        return engine

    publisher = pins_module.engine_publish(client_for, _publication_context)

    async def scenario() -> None:
        first = await pins.publish_for("tenant-a", publisher)
        assert first["definition_digest"] == DEFINITION
        assert await pins.publish_for("tenant-a", publisher) == first

    asyncio.run(scenario())
    ((params, _key),) = engine.sent
    component = params["op"]["request"]["component"]
    assert params["op"]["request"]["context"]["expected_revision"] == 0
    assert component["content_digest"] == pin["content_digest"]
    assert ds.canonical_body_bytes(
        ds.decode_body(component["content_digest"], component["attributes"])
    ) == ds.canonical_body_bytes(pin["body"])


# spec: ARBORA-RELEASE-R003.4.4
def test_engine_publish_sends_a_valid_request_and_reuses_a_current_entry(
    engine_export: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("epistemic_graph.generated.storage")
    pin = _pin()
    engine = _Engine()

    async def client_for(_tenant: str) -> Any:
        return engine

    publish = pins_module.engine_publish(client_for, _publication_context)
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
    digest = asyncio.run(
        pins_module.engine_publish(reuse_client, _publication_context)("t", pin)
    )
    assert digest == "sha256:" + "c" * 64 and reuse.sent == []

    stale = _Engine(
        current=SimpleNamespace(
            content_digest="sha256:" + "e" * 64,
            definition_digest="sha256:" + "f" * 64,
            entry_revision=7,
        )
    )

    async def stale_client(_tenant: str) -> Any:
        return stale

    digest = asyncio.run(
        pins_module.engine_publish(stale_client, _publication_context)("t", pin)
    )
    assert digest == DEFINITION
    ((stale_params, _stale_key),) = stale.sent
    assert stale_params["op"]["request"]["context"]["expected_revision"] == 7


@pytest.mark.spec("ARBORA-RELEASE-R003.4.4")
def test_engine_publish_binds_concurrent_tenants_and_restores_after_failure(
    engine_export: ModuleType,
) -> None:
    pin = _pin()
    contexts = {
        "tenant-a": _client_context("tenant-a", "service:a"),
        "tenant-b": _client_context("tenant-b", "service:b"),
    }
    clients: dict[str, _ScopedEngine] = {}

    async def connect(**kwargs: Any) -> Any:
        client = _ScopedEngine()
        clients[kwargs["graph_name"]] = client
        return client

    async def no_unbound_client(_tenant: str) -> Any:
        raise AssertionError("publisher used the unbound client path")

    pool = EpistemicClientPool(connect, auth_secret="test-secret")

    def binder(graph: str):
        @contextlib.asynccontextmanager
        async def bind(tenant: str):
            async with pool.bind(contexts[tenant], graph) as client:
                yield client

        return bind

    publish_a = pins_module.engine_publish(
        no_unbound_client,
        _publication_context,
        "tenant-a:graph",
        bind_client=binder("tenant-a:graph"),
    )
    publish_b = pins_module.engine_publish(
        no_unbound_client,
        _publication_context,
        "tenant-b:graph",
        bind_client=binder("tenant-b:graph"),
    )

    async def scenario() -> None:
        assert await asyncio.gather(
            publish_a("tenant-a", pin), publish_b("tenant-b", pin)
        ) == [DEFINITION, DEFINITION]
        assert all(
            seen["tenant"] == "tenant-a"
            for seen in clients["tenant-a:graph"].seen_contexts
        )
        assert all(
            seen["tenant"] == "tenant-b"
            for seen in clients["tenant-b:graph"].seen_contexts
        )
        assert all(
            seen["principal"] == "service:a"
            for seen in clients["tenant-a:graph"].seen_contexts
        )
        assert all(
            seen["principal"] == "service:b"
            for seen in clients["tenant-b:graph"].seen_contexts
        )
        clients["tenant-a:graph"].fail_publish = True
        with pytest.raises(RuntimeError, match="transport failed"):
            await publish_a("tenant-a", pin)
        assert clients["tenant-a:graph"].context is None
        assert clients["tenant-b:graph"].context is None

    asyncio.run(scenario())


# spec: ARBORA-RELEASE-R003.4.4
def test_plan_run_refuses_unavailable_without_a_verified_pin() -> None:
    context = SimpleNamespace(services={}, caller=SimpleNamespace(tenant="t"))
    with pytest.raises(OperationRefused) as refused:
        asyncio.run(plans._feature_schema(context, "t"))
    assert refused.value.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-RELEASE-R003.4.4")
def test_unpublished_tenant_refuses_unavailable_and_never_publishes() -> None:
    """A request only reads the pin; publication is the process lifecycle's."""
    calls: list[str] = []

    async def publish(tenant: str, _pin: Any) -> str:
        calls.append(tenant)
        return DEFINITION + tenant

    pins = pins_module.TaskPlanSchemaPins(_pin())
    context = SimpleNamespace(
        services={"task_plan_schema_pins": pins, "task_plan_schema_publish": publish},
        caller=SimpleNamespace(tenant="t"),
    )
    with pytest.raises(OperationRefused) as refused:
        asyncio.run(plans._feature_schema(context, "t"))
    assert refused.value.code == "UNAVAILABLE"
    assert "not yet published" in refused.value.details["reason"]
    assert calls == [] and pins.pin_for("t") is None

    asyncio.run(pins.publish_for("t", publish))
    assert asyncio.run(plans._feature_schema(context, "t")) == pins.pin_for("t")
    assert calls == ["t"]


@pytest.mark.spec("ARBORA-RELEASE-R003.4.4")
def test_startup_publishes_every_served_tenant_with_bounded_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failures = {"tenant-a": 2, "tenant-b": 0, "tenant-c": 99}
    calls: list[str] = []
    delays: list[float] = []

    async def publish(tenant: str, _pin: Any) -> str:
        calls.append(tenant)
        if failures[tenant] > 0:
            failures[tenant] -= 1
            raise RuntimeError("engine not ready")
        return DEFINITION + tenant

    async def sleep(delay: float) -> None:
        delays.append(delay)

    pins = pins_module.TaskPlanSchemaPins(_pin())
    result = asyncio.run(
        pins_module.publish_at_startup(
            pins,
            publish,
            ("tenant-a", "tenant-b", "tenant-a", "tenant-c"),
            attempts=3,
            initial_delay_s=0.5,
            sleep=sleep,
        )
    )
    assert result == {"tenant-a": True, "tenant-b": True, "tenant-c": False}
    assert calls == ["tenant-a"] * 3 + ["tenant-b"] + ["tenant-c"] * 3
    assert delays == [0.5, 1.0, 0.5, 1.0]
    published = pins.pin_for("tenant-a")
    assert published is not None
    assert published["definition_digest"] == DEFINITION + "tenant-a"
    assert pins.pin_for("tenant-c") is None
    assert "not published for tenant-c after 3 attempts" in caplog.text


# spec: ARBORA-RELEASE-R003.4.4
def test_committed_pin_matches_the_installed_engine() -> None:
    committed = pins_module.load_pin()
    installed = pins_module.generate_pin()
    assert pins_module.drift(committed, installed) == []
    assert committed["available"] == installed["available"]
    assert pins_module.verify(committed, installed) is None
