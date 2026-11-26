"""Policy bundle operations send the file unchanged and pass engine refusals through (ARBORA-DEPLOY-POLICY-R004)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_invoke_contract import FixtureRuntime, run, setup_services

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import policy
from arbora.api.registry import Registry, Surface
from tests.api._support import make_verified_caller

BUNDLE = """schema_version: 1
bundle_id: ops.readers
scope:
  kind: graph
  graph: tenant-graph
rules:
  - principal_group: readers
    action: read
    resource_scope: {graph: tenant-graph}
    effect: allow
"""


class Engine:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.refuse: str | None = None

    async def __call__(
        self, _ctx: Any, method: str, params: dict[str, Any], **_k: Any
    ) -> Any:
        self.sent.append((method, params))
        if self.refuse:
            raise EngineRefusal(self.refuse)
        return {"kind": "policy_compiled", "roles": 1, "grants": 1}


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from medulla.generated import models

    (tmp_path / "readers.yaml").write_text(BUNDLE)
    engine = Engine()
    monkeypatch.setattr(policy, "engine_send", engine)

    def declare(present: bool) -> None:
        if present:
            monkeypatch.setattr(models, "IdentityOpPolicy", object, raising=False)
        else:
            monkeypatch.delattr(models, "IdentityOpPolicy", raising=False)

    declare(True)

    class Runtime(FixtureRuntime):
        service_scopes = frozenset({"example:execute"})

        async def dispatch(self, op: Any, params: Any, context: Any) -> Any:
            shaped = SimpleNamespace(
                caller=context.caller,
                client=None,
                idempotency_key=context.idempotency_key,
                services={"policy_bundle_root": tmp_path},
            )
            handler = {
                "policy.bundle.validate": policy.handle_bundle_validate,
                "policy.bundle.bind": policy.handle_bundle_bind,
            }[op.id]
            return await handler(shaped, params, op)

    ops = {op.id: op for op in policy.operations()}
    services, *_ = setup_services(
        registry=Registry([ops["policy.bundle.validate"]]),
        runtime=Runtime(),
        external_effect_operations=frozenset(),
    )
    fields = {f: getattr(services, f) for f in services.__dataclass_fields__}
    fields["runtime"] = Runtime()
    return SimpleNamespace(
        engine=engine,
        declare=declare,
        services=type(services)(**fields),
        root=tmp_path,
        ops=ops,
    )


def _caller() -> Any:
    return make_verified_caller("policy:read")()


def _validate(world: Any, surface: Surface) -> Any:
    return run(
        world.services,
        supplied=_caller(),
        op_id="policy.bundle.validate",
        params={"name": "readers"},
        surface=surface,
    )


def _bind(world: Any, name: str = "readers") -> Any:
    context = SimpleNamespace(
        caller=_caller(),
        client=None,
        idempotency_key="k",
        services={"policy_bundle_root": world.root},
    )
    return asyncio.run(
        policy.handle_bundle_bind(
            context, {"name": name}, world.ops["policy.bundle.bind"]
        )
    )


# spec: ARBORA-DEPLOY-POLICY-R004
def test_bundle_ops_send_the_file_unchanged_and_decide_the_same_on_http_and_mcp(
    world: Any,
) -> None:
    http = _validate(world, Surface.HTTP)
    mcp = _validate(world, Surface.MCP)
    assert http.code == mcp.code == "OK" and http.value == mcp.value

    method, body = world.engine.sent[0]
    assert method == "Identity"
    assert body["op"]["family"] == "policy" and body["op"]["op"] == "validate"
    assert world.engine.sent[0] == world.engine.sent[1]
    sent = body["op"]["bundle"]
    assert sent["bundle_id"] == "ops.readers"
    assert sent["scope"] == {"graph": {"graph": "tenant-graph"}}
    assert sent["rules"][0]["principal_group"] == "readers"

    assert _bind(world)["reply"]["kind"] == "policy_compiled"
    assert world.engine.sent[-1][1]["op"]["op"] == "bind"
    bind = world.ops["policy.bundle.bind"]
    assert bind.effect.value == "admin" and bind.confirm.value == "console"

    # A malformed file never reaches the engine.
    (world.root / "bad.yaml").write_text(BUNDLE + "extra: 1\n")
    before = len(world.engine.sent)
    with pytest.raises(OperationRefused) as invalid:
        _bind(world, "bad")
    assert invalid.value.code == "POLICY_BUNDLE_INVALID"
    assert len(world.engine.sent) == before


# spec: ARBORA-DEPLOY-POLICY-R004
def test_scope_exclusivity_and_self_authorization_refusals_pass_through(
    world: Any,
) -> None:
    for code in ("POLICY_SCOPE_BOUND", "POLICY_SELF_AUTHORIZATION"):
        world.engine.refuse = code
        with pytest.raises(EngineRefusal) as refused:
            _bind(world)
        assert refused.value.code == code
        served = _validate(world, Surface.HTTP)
        assert served.code == code and served.source == "engine"


# spec: ARBORA-DEPLOY-POLICY-R004
def test_engine_without_the_policy_family_refuses_unavailable_naming_it(
    world: Any,
) -> None:
    world.declare(False)
    with pytest.raises(OperationRefused) as refused:
        _bind(world)
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "IdentityOpPolicy"
    assert world.engine.sent == []
