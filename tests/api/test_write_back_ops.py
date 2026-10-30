"""ARBORA-FLEET-R003.2: write-back is a typed, gated registry operation.

Every test composes the ledger and effect through the served bootstrap
(``_with_write_back``): the real ``EgEffectJournal`` holds the preview gate
and the commit receipt in a WorkItem test port, and the effect is one
governed write through the bound fleet gateway.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.ops import write_back
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Confirm, Effect, Idempotency, Registry, Surface
from arbora.mcp_server.served_invocation import _with_write_back
from tests.fleet.test_attended_proposal_effect_owner import EffectWorkItems

_PARAMS = {
    "connector": "sql",
    "target": "customers.email",
    "change": {"value": "a@example.com"},
    "idempotency_key": "wb-1",
}
_CLAIMS = {
    "agent_id": "arbora",
    "principal": "service:arbora",
    "tenant": "t1",
    "audience": "medulla",
    "policy_version": "p1",
    "scopes": ["work:write"],
}


class _Engine:
    """One Medulla control graph shared by every bootstrap composed over it."""

    def __init__(self) -> None:
        self.work_items = EffectWorkItems()

    def use_verified_context(self, claims: Any) -> Any:
        import contextlib

        return contextlib.nullcontext()


class _Pool:
    def __init__(self, engine: _Engine) -> None:
        self.engine = engine

    async def connected_client(self, context: Any, graph: str) -> Any:
        return self.engine


class _Gateway:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail = fail

    async def call(self, caller: Any, server: str, tool: str, args: Any, **kw: Any):
        self.calls.append((caller.principal, server, tool, dict(args), kw))
        if self.fail:
            raise RuntimeError("connector fault")
        return {"id": len(self.calls)}


def _bootstrap(engine: _Engine, gateway: _Gateway) -> Any:
    context = SimpleNamespace(to_claims=lambda: dict(_CLAIMS))
    return _with_write_back(
        {"fleet_gateway": gateway},
        _Pool(engine),
        lambda tenant: context,
        lambda: SimpleNamespace(tenant="t1"),
    )


def _ctx(bound: Any, principal: str = "p1") -> Any:
    return SimpleNamespace(
        caller=SimpleNamespace(tenant="t1", principal=principal),
        services=bound,
        idempotency_key=_PARAMS["idempotency_key"],
    )


_OPS = {o.id: o for o in write_back.operations()}


async def _preview(ctx: Any, params: Any = _PARAMS) -> Any:
    return await write_back.handle_write_back_preview(
        ctx, params, _OPS["fleet.write_back.preview"]
    )


async def _commit(ctx: Any, params: Any = _PARAMS) -> Any:
    return await write_back.handle_write_back_commit(
        ctx, params, _OPS["fleet.write_back.commit"]
    )


def test_bootstrap_binds_nothing_without_the_fleet_gateway() -> None:
    assert _with_write_back({}, None, lambda t: t, lambda: None) == {}


@pytest.mark.spec("ARBORA-FLEET-R003.2")
async def test_preview_then_commit_and_replay_returns_original_receipt() -> None:
    engine, gateway = _Engine(), _Gateway()
    bound = _bootstrap(engine, gateway)
    preview = await _preview(_ctx(bound))
    assert preview["value"]["applied"] is False and gateway.calls == []
    first = await _commit(_ctx(bound))
    assert first["value"]["receipt"] == {"id": 1}
    assert gateway.calls[0][:4] == (
        "p1",
        "sql",
        "customers.email",
        {"value": "a@example.com"},
    )
    assert gateway.calls[0][4] == {"expected_effect": Effect.WRITE}
    # A fresh bootstrap over the same Medulla (a restart) replays the receipt.
    again = await _commit(_ctx(_bootstrap(engine, gateway)))
    assert again == first and len(gateway.calls) == 1


@pytest.mark.spec("ARBORA-FLEET-R003.2")
async def test_commit_without_matching_preview_or_with_mismatch_is_refused() -> None:
    engine, gateway = _Engine(), _Gateway()
    bound = _bootstrap(engine, gateway)
    with pytest.raises(write_back.WriteBackRefused):
        await _commit(_ctx(bound))
    await _preview(_ctx(bound), {**_PARAMS, "idempotency_key": "wb-2"})
    with pytest.raises(write_back.WriteBackRefused):
        await _commit(
            _ctx(bound), {**_PARAMS, "idempotency_key": "wb-2", "change": {"v": 2}}
        )
    # Another principal's preview never admits this caller's commit.
    await _preview(_ctx(bound, "p2"), {**_PARAMS, "idempotency_key": "wb-3"})
    with pytest.raises(write_back.WriteBackRefused):
        await _commit(_ctx(bound), {**_PARAMS, "idempotency_key": "wb-3"})
    assert gateway.calls == []


async def test_faulted_connector_write_is_not_retried_by_replay() -> None:
    engine = _Engine()
    bound = _bootstrap(engine, _Gateway(fail=True))
    await _preview(_ctx(bound))
    with pytest.raises(RuntimeError):
        await _commit(_ctx(bound))
    healthy = _Gateway()
    with pytest.raises(write_back.WriteBackRefused):
        await _commit(_ctx(_bootstrap(engine, healthy)))
    assert healthy.calls == []


def test_write_back_ops_are_typed_gated_registry_entries() -> None:
    registry = Registry(write_back.operations())
    commit = registry["fleet.write_back.commit"]
    assert commit.confirm is Confirm.PLAN
    assert commit.idempotency is Idempotency.KEY_REQUIRED
    assert Surface.MCP in commit.surfaces
    served = get_registry()
    assert served.get("fleet.write_back.preview") is not None
    assert served.get("fleet.write_back.commit") is not None
