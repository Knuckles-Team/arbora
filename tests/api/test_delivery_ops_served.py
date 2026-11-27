"""Autonomous deliveries are read and steered through served operations (ARBORA-OPS-R896.7, R896.10).

The engine holds every delivery record and decision; the recording engine below stands in
for the delivery methods engine ``main`` serves (the pinned release declares none, so
``route_methods`` declares them for the test). Arbora keeps no delivery state.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from medulla import EngineResponseError

from arbora.api.ops import development_delivery as delivery
from tests.api._served_support import confirmable_invocation, person, route_methods
from tests.test_served_operation_api import _call, _value, served  # noqa: F401

KEY = "Idempotency-Key"
SCOPES = ("lifecycle:write",)
_PRINCIPAL = "person:one"


class DeliveryEngine:
    """The delivery records of ``tenant:one``; another tenant's id answers as absent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = {
            "d1": {
                "id": "d1",
                "stage": "person/answer",
                "scope_rows": ["R1"],
                "proposals": [],
            },
            "d2": {
                "id": "d2",
                "stage": "working",
                "scope_rows": ["R2"],
                "proposals": [],
            },
        }

    def _delivery(self, params: dict[str, Any]) -> dict[str, Any]:
        row = self.rows.get(params["request"]["delivery_id"])
        if row is None:
            raise EngineResponseError("DELIVERY_NOT_FOUND", "no such delivery")
        return row

    def list(self, params: dict[str, Any], _who: str) -> Any:
        self.calls.append(("DeliveryList", params))
        return list(self.rows.values())

    def status(self, params: dict[str, Any], _who: str) -> Any:
        self.calls.append(("DeliveryStatus", params))
        row = self._delivery(params)
        return {
            "stage": row["stage"],
            "mandate": {"strength": "owner", "scope_rows": row["scope_rows"]},
            "proposals": row["proposals"],
            "attention": {"text": "which branch?"} if row["id"] == "d1" else None,
            "ended": None,
        }

    def stop(self, params: dict[str, Any], _who: str) -> Any:
        self.calls.append(("DeliveryStop", params))
        self._delivery(params)["stage"] = "ended"
        return {"outcome": "stopped"}

    def retry(self, params: dict[str, Any], _who: str) -> Any:
        self.calls.append(("DeliveryRetry", params))
        self._delivery(params)
        return {"assignment_id": "a2"}

    def answer(self, params: dict[str, Any], _who: str) -> Any:
        self.calls.append(("DeliveryRespond", params))
        self._delivery(params)
        return {"accepted": True}


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> Any:
    return confirmable_invocation(monkeypatch)


@pytest.fixture
def env(stack: Any, served: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    app, client = served
    engine = DeliveryEngine()
    route_methods(
        client,
        {
            "DeliveryList": engine.list,
            "DeliveryStatus": engine.status,
            "DeliveryStop": engine.stop,
            "DeliveryRetry": engine.retry,
            "DeliveryRespond": engine.answer,
        },
        monkeypatch,
        {"fixture-verified": person("one", extra_scopes=SCOPES)},
    )
    return app, engine, stack, monkeypatch


@pytest.mark.spec("ARBORA-OPS-R896.7")
def test_delivery_ops_read_and_steer_without_local_state(env: Any) -> None:
    app, engine, *_ = env
    listed = _value(_call(app, "development.delivery.list", {"repository": "repo"}))
    assert [row["id"] for row in listed["items"]] == ["d1", "d2"]
    assert engine.calls[0][1] == {"request": {"repo": "repo"}}
    waiting = _value(_call(app, "development.delivery.list", {"stage": "working"}))
    assert [row["id"] for row in waiting["items"]] == ["d2"]
    status = _value(_call(app, "development.delivery.status", {"delivery_id": "d1"}))
    assert status["stage"] == "person/answer" and status["attention"] == {
        "text": "which branch?"
    }
    answered = _value(
        _call(
            app,
            "development.delivery.answer",
            {"delivery_id": "d1", "text": "main"},
            **{KEY: "a1"},
        )
    )
    assert answered == {"delivery_id": "d1", "accepted": True}
    sent = [p for n, p in engine.calls if n == "DeliveryRespond"][0]["request"]
    assert sent == {"delivery_id": "d1", "answer": "main"}
    retried = _value(
        _call(app, "development.delivery.retry", {"delivery_id": "d2"}, **{KEY: "r1"})
    )
    trace = [p for n, p in engine.calls if n == "DeliveryRetry"][0]["request"][
        "run_trace_id"
    ]
    assert retried == {"delivery_id": "d2", "run_trace_id": trace}
    # The trace id is fixed by the idempotency key, so a replayed retry names the same run.
    again = _value(
        _call(app, "development.delivery.retry", {"delivery_id": "d2"}, **{KEY: "r1"})
    )
    assert again["run_trace_id"] == trace
    stopped = _value(
        _call(app, "development.delivery.stop", {"delivery_id": "d2"}, **{KEY: "s1"})
    )
    assert stopped == {"delivery_id": "d2", "outcome": "stopped"}
    # Arbora holds no stage: the next read is the engine's.
    after = _value(_call(app, "development.delivery.status", {"delivery_id": "d2"}))
    assert after["stage"] == "ended"


@pytest.mark.spec("ARBORA-OPS-R896.7")
def test_a_foreign_or_unknown_delivery_answers_the_same_refusal(env: Any) -> None:
    app, engine, *_ = env
    absent = _call(
        app, "development.delivery.status", {"delivery_id": "d-other-tenant"}
    )
    assert absent.status_code == 404
    assert absent.json()["error"]["code"] == "DELIVERY_NOT_FOUND"
    stop = _call(
        app,
        "development.delivery.stop",
        {"delivery_id": "d-other-tenant"},
        **{KEY: "s9"},
    )
    assert stop.json()["error"]["code"] == "DELIVERY_NOT_FOUND"


@pytest.mark.spec("ARBORA-OPS-R896.7")
def test_stop_and_answer_admit_only_a_human_principal() -> None:
    """The one principal rule of the invoke pipeline refuses a service or agent caller."""
    from types import SimpleNamespace

    from arbora.api.invoke.steps import principal_rule

    ops = {op.id: op for op in delivery.operations()}
    service = SimpleNamespace(principal_kind="service", delegated=False)
    person_caller = SimpleNamespace(principal_kind="human", delegated=False)
    for op_id in ("development.delivery.stop", "development.delivery.answer"):
        refusal = principal_rule(ops[op_id], service)  # type: ignore[arg-type]
        assert refusal is not None and refusal.code == "PRINCIPAL_NOT_ALLOWED"
        assert principal_rule(ops[op_id], person_caller) is None  # type: ignore[arg-type]
    # Reads and a retry are open to any admitted principal with the scope.
    for op_id in ("development.delivery.status", "development.delivery.retry"):
        assert principal_rule(ops[op_id], service) is None  # type: ignore[arg-type]


class _Receipts:
    """The owner receipts of ARBORA-OPS-R863, bound to one head commit."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    async def verify(self, ref: str, *, head_sha: str, tenant: str) -> str:
        self.asked.append((ref, head_sha))
        if ref != "receipt:owner":
            return "missing"
        return "ok" if head_sha == "abc1234" else "head_mismatch"


class _Connector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool: str, arguments: dict[str, Any]) -> None:
        self.calls.append((tool, arguments))


def _arming_record(verdict: str, request: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256(_PRINCIPAL.encode()).hexdigest()
    return {
        "caller_principal": f"principal:sha256:{digest}",
        "tenant_id": request["tenant_id"],
        "question": request["question"],
        "inputs": {"params": request["params"]},
        "record_id": "decision:1",
        "record_digest": "d",
        "answer": {"type": "merge_arming", "value": {"verdict": verdict}},
    }


def _arm_context(
    monkeypatch: pytest.MonkeyPatch, verdict: str, sent: list[dict[str, Any]]
) -> tuple[Any, _Connector]:
    from types import SimpleNamespace

    connector = _Connector()
    caller = SimpleNamespace(tenant="tenant:one", principal=_PRINCIPAL)
    services = {
        "owner_receipts": _Receipts(),
        "delivery_connector": connector,
        "task_plan_feature_schema": {"feature_schema": "pin"},
    }
    context = SimpleNamespace(caller=caller, services=services, idempotency_key="k")

    async def engine_send(
        ctx: Any, method: str, params: dict[str, Any], **_k: Any
    ) -> Any:
        sent.append(params)
        return {"records": [_arming_record(verdict, params["request"])]}

    monkeypatch.setattr(delivery, "engine_send", engine_send)
    return context, connector


@pytest.mark.spec("ARBORA-OPS-R896.10")
async def test_arm_needs_owner_receipt_on_head_and_platform_never_merges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.invoke.pipeline import OperationRefused

    if True:
        sent: list[dict[str, Any]] = []
        context, connector = _arm_context(monkeypatch, "arm", sent)
        base = {"delivery_id": "d1", "proposal": "p1", "head_sha": "abc1234"}
        for params, code in (
            (base, "MERGE_APPROVAL_REQUIRED"),
            ({**base, "approval_ref": "receipt:other"}, "MERGE_APPROVAL_REQUIRED"),
            (
                {**base, "approval_ref": "receipt:owner", "head_sha": "ffff999"},
                "MERGE_HEAD_MISMATCH",
            ),
        ):
            with pytest.raises(OperationRefused) as refused:
                await delivery.handle_delivery_arm(context, params, None)  # type: ignore[arg-type]
            assert refused.value.code == code
        # No receipt on the exact head: no engine decision and no connector call at all.
        assert sent == [] and connector.calls == []
        good = {**base, "approval_ref": "receipt:owner"}
        armed = await delivery.handle_delivery_arm(context, good, None)  # type: ignore[arg-type]
        assert armed == {"verdict": "arm", "decision_id": "decision:1"}
        assert connector.calls == [
            (
                "arm_merge",
                {
                    "proposal": "p1",
                    "expected_head": "abc1234",
                    "approval_ref": "receipt:owner",
                },
            )
        ]
        question = sent[0]["request"]["question"]
        assert (
            question["kind"] == "merge_arming" and question["safety"] == "irreversible"
        )
        held_context, held = _arm_context(monkeypatch, "hold", [])
        assert (await delivery.handle_delivery_arm(held_context, good, None))[
            "verdict"
        ] == "hold"  # type: ignore[arg-type]
        assert held.calls == []
        off_context, off = _arm_context(monkeypatch, "disarm", [])
        await delivery.handle_delivery_arm(off_context, good, None)  # type: ignore[arg-type]
        assert [tool for tool, _ in off.calls] == ["disarm_merge"]


@pytest.mark.spec("ARBORA-OPS-R896.10")
async def test_arm_without_composed_receipts_or_connector_refuses_unavailable() -> None:
    from types import SimpleNamespace

    from arbora.api.invoke.pipeline import OperationRefused

    caller = SimpleNamespace(tenant="tenant:one", principal=_PRINCIPAL)
    context = SimpleNamespace(caller=caller, services={}, idempotency_key="k")
    params = {
        "delivery_id": "d1",
        "proposal": "p1",
        "head_sha": "abc1234",
        "approval_ref": "r",
    }
    with pytest.raises(OperationRefused) as refused:
        await delivery.handle_delivery_arm(context, params, None)  # type: ignore[arg-type]
    assert refused.value.code == "UNAVAILABLE"


class _KvEngine:
    """The engine key-value store: versioned entries, compare-and-set."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[int, bytes]] = {}

    async def __call__(
        self, ctx: Any, method: str, params: dict[str, Any], **_k: Any
    ) -> Any:
        key = params["key"]
        version, value = self.store.get(key, (0, None))
        if method == "KvGet":
            return {"value": value, "version": version} if value is not None else None
        assert method == "KvCas"
        if version != params["expected_version"]:
            return {"swapped": False, "version": version}
        self.store[key] = (version + 1, params["new"])
        return {"swapped": True, "version": version + 1}


class _EffectFleet:
    """The connector: applies an effect once per key, can lose its answer, and reads by key."""

    def __init__(self) -> None:
        self.applied: dict[str, dict[str, str]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.lose_answer = False

    async def __call__(self, server: str, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((tool, arguments))
        if tool == "effect_status":
            key = arguments["idempotency_key"]
            if key in self.applied:
                return {"state": "applied", "receipt": self.applied[key]}
            return {"state": "absent"}
        key = arguments["request"]["idempotency_key"]
        receipt = self.applied.setdefault(
            key,
            {
                "effect_id": f"e{len(self.applied) + 1}",
                "provider_ref": "pr#7/comment/1",
                "applied_at": "2026-11-26T10:00:00Z",
            },
        )
        if self.lose_answer:
            from arbora.api.domain.fleet.service_child import ServiceChildOutcomeUnknown

            raise ServiceChildOutcomeUnknown("lost")
        return {"receipt": receipt}


@pytest.mark.spec("ARBORA-OPS-R896.11")
async def test_delivery_writes_are_idempotent_and_uncertain_effects_reconcile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from arbora.api.ops import development_effects as effects
    from arbora.api.ops.research import FleetCaller

    kv, fleet = _KvEngine(), _EffectFleet()
    monkeypatch.setattr(effects, "engine_send", kv)
    monkeypatch.setattr(FleetCaller, "call", lambda self, s, t, a: fleet(s, t, a))
    context = SimpleNamespace(caller=SimpleNamespace(tenant="tenant:one"), services={})
    base = {
        "delivery_id": "d1",
        "assignment_id": "a1",
        "step": "comment-1",
        "effect_kind": "comment",
        "target": "org/repo#7",
        "payload_digest": "sha256:" + "a" * 64,
        "server": "github",
        "tool": "post_comment",
    }
    key = effects.delivery_effect_key("d1", "a1", "comment-1")
    assert key == "d1:a1:comment-1"
    first = await effects.handle_delivery_effect(context, base, None)
    again = await effects.handle_delivery_effect(context, base, None)
    # The effect applies once; the replay returns the same receipt and writes nothing.
    assert first == again and first["state"] == "done"
    assert len([c for c in fleet.calls if c[0] == "post_comment"]) == 1
    sent = fleet.calls[0][1]["request"]
    assert sent["idempotency_key"] == key and sent["effect_kind"] == "comment"


@pytest.mark.spec("ARBORA-OPS-R896.11")
async def test_a_lost_effect_answer_is_uncertain_until_reconciled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.api.ops import development_effects as effects
    from arbora.api.ops.research import FleetCaller

    kv, fleet = _KvEngine(), _EffectFleet()
    monkeypatch.setattr(effects, "engine_send", kv)
    monkeypatch.setattr(FleetCaller, "call", lambda self, s, t, a: fleet(s, t, a))
    context = SimpleNamespace(caller=SimpleNamespace(tenant="tenant:one"), services={})
    base = {
        "delivery_id": "d1",
        "assignment_id": "a1",
        "step": "comment-1",
        "effect_kind": "comment",
        "target": "org/repo#7",
        "payload_digest": "sha256:" + "a" * 64,
        "server": "github",
        "tool": "post_comment",
    }
    # A lost answer leaves the effect pending: uncertain, and a retry refuses.
    lost = {**base, "step": "comment-2"}
    fleet.lose_answer = True
    with pytest.raises(OperationRefused) as uncertain:
        await effects.handle_delivery_effect(context, lost, None)
    assert uncertain.value.code == "EFFECT_UNCERTAIN"
    fleet.lose_answer = False
    with pytest.raises(OperationRefused) as retried:
        await effects.handle_delivery_effect(context, lost, None)
    assert retried.value.code == "EFFECT_UNCERTAIN"
    assert len([c for c in fleet.calls if c[0] == "post_comment"]) == 1
    # Reconciliation reads the connector: the effect was applied, so it settles done and
    # the retry returns its receipt without a second write.
    probe = {k: lost[k] for k in ("delivery_id", "assignment_id", "step")}
    settled = await effects.handle_delivery_effect_reconcile(
        context, {**probe, "server": "github", "tool": "effect_status"}, None
    )
    assert settled["state"] == "done" and settled["receipt"]["provider_ref"]
    final = await effects.handle_delivery_effect(context, lost, None)
    assert final["receipt"] == settled["receipt"]
    assert len([c for c in fleet.calls if c[0] == "post_comment"]) == 1


@pytest.mark.spec("ARBORA-OPS-R896.11")
async def test_an_effect_the_provider_never_applied_reconciles_absent_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from arbora.api.ops import development_effects as effects
    from arbora.api.ops.research import FleetCaller

    kv, fleet = _KvEngine(), _EffectFleet()
    monkeypatch.setattr(effects, "engine_send", kv)
    monkeypatch.setattr(FleetCaller, "call", lambda self, s, t, a: fleet(s, t, a))
    context = SimpleNamespace(caller=SimpleNamespace(tenant="tenant:one"), services={})
    base = {
        "delivery_id": "d1",
        "assignment_id": "a1",
        "step": "comment-3",
        "effect_kind": "comment",
        "target": "org/repo#7",
        "payload_digest": "sha256:" + "a" * 64,
        "server": "github",
        "tool": "post_comment",
    }
    never = base
    kv.store[effects.delivery_effect_key("d1", "a1", "comment-3")] = (
        1,
        b'{"state": "pending"}',
    )
    gone = await effects.handle_delivery_effect_reconcile(
        context,
        {
            "delivery_id": "d1",
            "assignment_id": "a1",
            "step": "comment-3",
            "server": "github",
            "tool": "effect_status",
        },
        None,
    )
    assert gone["state"] == "absent"
    # The reservation reads absent, so the retry applies.
    assert (await effects.handle_delivery_effect(context, never, None))[
        "state"
    ] == "done"
