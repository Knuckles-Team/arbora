"""Operator hooks around the invoke pipeline (ARBORA-OPS-R009.6 to R009.9)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from arbora.api.invoke import OpError, OpResult, invoke
from arbora.api.invoke.hooks import (
    OPERATOR_HOOK_TIMEOUT_S,
    HookChain,
    HookConfigError,
    HookPoint,
    HookSpec,
    host_hook_invoker,
    load_hooks,
)
from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    OpSpec,
    Registry,
    Surface,
    Verb,
)
from tests.api._hook_fixtures import SEEN
from tests.api._support import make_verified_caller
from tests.api.test_invoke_contract import FixtureRuntime, operation, setup_services
from tests.test_served_operation_api import served  # noqa: F401

SECRET = "tok-" + "9f8e7d6c5b4a"  # sanitizer:ignore - synthetic
SURFACES = [Surface.HTTP, Surface.MCP]
hook_caller = make_verified_caller("hook:run")


class HookParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op_id: str
    caller_tenant: str
    params_json: str | None = None
    result_json: str | None = None


def hook_op(op_id: str, handler: str = "fixture.hook") -> OpSpec:
    return OpSpec(
        id=op_id,
        verb=Verb.FIND,
        summary="Fixture hook",
        examples=("fixture hook",),
        params=HookParams,
        result=HookParams,
        binding=Composite(handler=handler),
        scopes=frozenset({"hook:run"} if handler == "fixture.hook" else ()),
        effect=Effect.READ,
        audit=AuditClass.NONE,
        surfaces=frozenset(Surface),
    )


class HookRuntime(FixtureRuntime):
    """Answers each hook operation from a table; the rest as the base fixture."""

    def __init__(self) -> None:
        super().__init__()
        self.answers: dict[str, Any] = {}
        self.seen: dict[str, list[dict[str, Any]]] = {}
        self.hook_callers: list[Any] = []

    async def dispatch(self, op, params, context):
        if op.id in self.answers:
            self.seen.setdefault(op.id, []).append(dict(params))
            self.hook_callers.append(context.caller)
            answer = self.answers[op.id]
            if isinstance(answer, BaseException):
                raise answer
            return await answer(params) if callable(answer) else answer
        return await super().dispatch(op, params, context)


def build(*specs: HookSpec, timeout_s: float = OPERATOR_HOOK_TIMEOUT_S):
    ops = [operation(), hook_op("hooks.one"), hook_op("hooks.two")]
    runtime = HookRuntime()
    chain = HookChain(specs, timeout_s=timeout_s)
    services, _, audit, journal, _ = setup_services(
        registry=Registry(ops), runtime=runtime, hooks=chain
    )
    chain.bind(
        host_hook_invoker(
            services,
            lambda tenant, request_id: hook_caller(
                tenant=tenant, request_id=request_id, principal_kind="service"
            ),
        )
    )
    return services, runtime, audit, chain


def call(services, surface=Surface.HTTP, key="k1", **params):
    caller = make_verified_caller("example:write")()
    return asyncio.run(
        invoke(
            "example.write",
            params or {"subject": "object:one"},
            caller,
            surface,
            services=services,
            idempotency_key=key,
        )
    )


def write_hooks(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps({"hooks": rows}), encoding="utf-8")
    return path


ONE_IN = HookSpec("one", HookPoint.INPUT, "hooks.one")
TWO_IN = HookSpec("two", HookPoint.INPUT, "hooks.two")
ONE_OUT = HookSpec("one-out", HookPoint.OUTPUT, "hooks.one")


@pytest.mark.spec("ARBORA-OPS-R009.6")
def test_bootstrap_refuses_absent_write_or_recursive_hook(tmp_path: Path) -> None:
    registry = Registry([operation(), hook_op("hooks.one")])
    for row, reason in (
        ({"name": "a", "point": "input", "op_ref": "hooks.absent"}, "registered"),
        ({"name": "a", "point": "input", "op_ref": "example.write"}, "effect"),
    ):
        path = write_hooks(tmp_path / "hooks.json", [row])
        with pytest.raises(HookConfigError, match=reason) as refused:
            load_hooks(registry, path=path)
        assert refused.value.reason == "hook_invalid"
    path = write_hooks(
        tmp_path / "hooks.json",
        [
            {"name": "a", "point": "input", "op_ref": "hooks.one"},
            {"name": "b", "point": "output", "op_ref": "hooks.one"},
        ],
    )
    with pytest.raises(HookConfigError, match="already a hook"):
        load_hooks(registry, path=path)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(HookConfigError):
        load_hooks(registry, path=bad)
    assert not load_hooks(registry, path=tmp_path / "absent.json")


@pytest.mark.spec("ARBORA-OPS-R009.6")
def test_hooks_load_in_declared_order(tmp_path: Path, monkeypatch) -> None:
    registry = Registry([operation(), hook_op("hooks.one"), hook_op("hooks.two")])
    path = write_hooks(
        tmp_path / "operator_hooks.json",
        [
            {"name": "second", "point": "input", "op_ref": "hooks.two"},
            {"name": "first", "point": "output", "op_ref": "hooks.one",
             "include_errors": True},
        ],
    )  # fmt: skip
    chain = load_hooks(registry, path=path)
    assert [h.name for h in chain.hooks] == ["second", "first"]
    assert chain.hooks[1].include_errors and not chain.hooks[0].include_errors
    assert chain.timeout_s == OPERATOR_HOOK_TIMEOUT_S == 2.0
    monkeypatch.setenv("ARBORA_CONFIG_DIR", str(tmp_path))
    assert [h.name for h in load_hooks(registry).hooks] == ["second", "first"]


@pytest.mark.spec("ARBORA-OPS-R009.7")
@pytest.mark.parametrize("surface", SURFACES)
def test_input_hook_allow_redact_and_reject(surface) -> None:
    services, runtime, audit, _ = build(ONE_IN, TWO_IN)
    runtime.answers["hooks.one"] = {"decision": "allow"}
    runtime.answers["hooks.two"] = {"decision": "allow"}
    allowed = call(services, surface, key="a", subject="object:one")
    assert isinstance(allowed, OpResult)
    # The hook sees the caller's tenant and the params, as the service principal.
    seen = runtime.seen["hooks.one"][0]
    assert seen["caller_tenant"] == "tenant:one"
    assert json.loads(seen["params_json"])["subject"] == "object:one"
    assert all(c.principal_kind == "service" for c in runtime.hook_callers)

    runtime.answers["hooks.one"] = {
        "decision": "redact",
        "spans": [{"path": "subject", "start": 7, "end": 10}],
    }
    redacted = call(services, surface, key="b", subject="object:one")
    assert isinstance(redacted, OpResult)
    # The next hook receives the previous hook's output.
    assert json.loads(runtime.seen["hooks.two"][-1]["params_json"])["subject"] == (
        "object:[redacted]"
    )
    assert runtime.calls[-1][1]["subject"] == "object:[redacted]"

    runtime.answers["hooks.two"] = {"decision": "reject", "code": "POLICY_DENIED"}
    before = len(runtime.calls)
    rejected = call(services, surface, key="c", subject="object:one")
    assert isinstance(rejected, OpError) and rejected.code == "POLICY_DENIED"
    assert rejected.details == {"hook": "two"}
    runtime.answers["hooks.two"] = {"decision": "reject"}
    assert call(services, surface, key="d").code == "HOOK_REJECTED"
    assert len(runtime.calls) == before


@pytest.mark.spec("ARBORA-OPS-R009.7")
def test_a_redaction_must_still_pass_argument_validation() -> None:
    services, runtime, _, _ = build(ONE_IN)
    runtime.answers["hooks.one"] = {
        "decision": "redact",
        "spans": [{"path": "value", "start": 0, "end": 1}],
    }
    refused = call(services, key="e", subject="x", value=5)
    assert isinstance(refused, OpError)
    assert refused.code == "HOOK_FAILED"  # a span over a non-string rejects, closed


@pytest.mark.spec("ARBORA-OPS-R009.8")
@pytest.mark.parametrize("surface", SURFACES)
def test_output_hook_redacts_and_rejects_and_replay_matches(surface) -> None:
    services, runtime, audit, _ = build(ONE_OUT)
    runtime.answers["hooks.one"] = {
        "decision": "redact",
        "spans": [{"path": "payload.note", "start": 0, "end": 4}],
    }

    async def answer():
        return {"payload": {"note": "abcd-visible"}}

    runtime.on_dispatch = answer
    first = call(services, surface, key="o1", subject="object:one")
    assert isinstance(first, OpResult)
    assert first.value == {"payload": {"note": "[redacted]-visible"}}
    replayed = call(services, surface, key="o1", subject="object:one")
    assert replayed == first
    assert len(runtime.calls) == 1

    runtime.answers["hooks.one"] = {"decision": "reject", "code": "POLICY_DENIED"}
    rejected = call(services, surface, key="o2", subject="object:one")
    assert isinstance(rejected, OpError) and rejected.code == "POLICY_DENIED"
    assert rejected.details == {"hook": "one-out", "effect_applied": True}
    assert audit.events[-1]["result_status"] == "POLICY_DENIED"
    assert call(services, surface, key="o2", subject="object:one") == rejected


@pytest.mark.spec("ARBORA-OPS-R009.8")
def test_an_error_result_passes_an_output_hook_only_with_include_errors() -> None:
    async def failing():
        raise RuntimeError("boom")

    for include, calls in ((False, 0), (True, 1)):
        spec = HookSpec("one-out", HookPoint.OUTPUT, "hooks.one", include)
        services, runtime, _, _ = build(spec)
        runtime.answers["hooks.one"] = {"decision": "allow"}
        runtime.on_dispatch = failing
        result = call(services, key="x")
        assert isinstance(result, OpError)
        assert len(runtime.seen.get("hooks.one", [])) == calls


@pytest.mark.spec("ARBORA-OPS-R009.9")
def test_timeout_error_and_unknown_decision_reject_with_hook_failed() -> None:
    never = asyncio.Event()

    async def hang(_params):
        await never.wait()

    for index, answer in enumerate(
        (
            hang,
            RuntimeError("raises"),
            {"decision": "maybe"},
            {"decision": "redact"},
            "str",
        )
    ):
        services, runtime, audit, _ = build(ONE_IN, TWO_IN, timeout_s=0.01)
        runtime.answers["hooks.one"] = answer
        runtime.answers["hooks.two"] = {"decision": "allow"}
        refused = call(services, key=f"f{index}")
        assert isinstance(refused, OpError), answer
        assert refused.code == "HOOK_FAILED"
        assert refused.details == {"hook": "one"}
        assert "hooks.two" not in runtime.seen  # no later hook runs
        assert runtime.calls == []


@pytest.mark.spec("ARBORA-OPS-R009.9")
def test_decisions_are_recorded_without_matched_text() -> None:
    clock = iter(float(n) for n in range(100))
    ticks = lambda: next(clock)  # noqa: E731 - a controlled clock
    services, runtime, audit, chain = build(ONE_IN, ONE_OUT)
    chain.clock = ticks
    runtime.answers["hooks.one"] = {
        "decision": "redact",
        "spans": [{"path": "payload.note", "start": 0, "end": 12}],
    }

    async def answer():
        return {"payload": {"note": SECRET}}

    runtime.on_dispatch = answer
    result = call(services, key="r1", subject="object:one", payload={"note": SECRET})
    assert isinstance(result, OpResult)
    event = audit.events[-1]
    records = json.loads(event["hooks"])
    assert [(r["name"], r["point"], r["decision"]) for r in records] == [
        ("one", "input", "redact"),
        ("one-out", "output", "redact"),
    ]
    assert all(r["duration_ms"] == 1000 for r in records)
    assert SECRET not in repr(audit.events)
    assert event["result_status"] == "OK"
    from arbora.api.errors import ArboraErrorCode

    assert {ArboraErrorCode.HOOK_FAILED, ArboraErrorCode.HOOK_REJECTED}


@pytest.mark.spec("ARBORA-OPS-R009.9")
def test_no_hooks_leave_the_audit_event_unchanged() -> None:
    services, runtime, audit, _ = build()
    assert isinstance(call(services, key="n1"), OpResult)
    assert "hooks" not in audit.events[-1]


@pytest.fixture
def hooked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The served stack with two input hooks and one output hook configured."""
    from arbora.api.ops import registry_factory
    from tests.api._served_support import capture_invocation

    me = "tests.api._hook_fixtures"
    extra = [
        hook_op("hooks.one", f"{me}.hook_input"),
        hook_op("hooks.two", f"{me}.hook_second"),
        hook_op("hooks.out", f"{me}.hook_output"),
    ]
    real = registry_factory.get_registry

    def with_hooks() -> Registry:
        return Registry([*real(), *extra])

    monkeypatch.setattr(registry_factory, "get_registry", with_hooks)
    write_hooks(
        tmp_path / "operator_hooks.json",
        [
            {"name": "one", "point": "input", "op_ref": "hooks.one"},
            {"name": "two", "point": "input", "op_ref": "hooks.two"},
            {"name": "out", "point": "output", "op_ref": "hooks.out"},
        ],
    )
    monkeypatch.setenv("ARBORA_CONFIG_DIR", str(tmp_path))
    SEEN.clear()
    return capture_invocation(monkeypatch)


@pytest.mark.spec("ARBORA-OPS-R009.9")
def test_two_hooks_through_http_and_mcp(hooked: Any, served: Any, monkeypatch) -> None:  # noqa: F811
    from tests.api._served_support import McpAsk, person
    from tests.test_served_operation_api import _call, _value

    app, client = served
    key = {"Idempotency-Key": "hot-1"}
    written = _value(
        _call(
            app, "memory.write", {"key": "n", "value": {"text": "a secret note"}}, **key
        )
    )
    assert written["swapped"] is True
    # Both hooks ran in order; the second saw the first one's redaction.
    assert SEEN == [("one", "a secret note"), ("two", "a [redacted] note")]
    stored = [p for m, p, _g in client.sent if m == "KvPut"]
    assert stored and "secret" not in json.dumps(stored, default=str)

    denied = _call(app, "memory.read", {"key": "n"})
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "POLICY_DENIED"

    ask = McpAsk(hooked["invocation"], monkeypatch, person("one"))
    refused = ask("ask", "memory.read", {"key": "n"})
    assert refused["ok"] is False and refused["error"]["code"] == "POLICY_DENIED"
    mcp_write = ask(
        "act", "memory.write", {"key": "m", "value": {"text": "another secret"}},
        idempotency_key="hot-2",
    )  # fmt: skip
    assert mcp_write["ok"] is True, mcp_write
    assert SEEN[-1] == ("two", "another [redacted]")
