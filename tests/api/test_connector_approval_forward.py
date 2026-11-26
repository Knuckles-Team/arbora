"""The approval reference reaches every connector call path (ARBORA-OPS-R009.5)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.plan import approval_id_for
from arbora.api.ops import fleet
from arbora.fleet import multiplexer as multiplexer_module
from arbora.fleet.approval_meta import APPROVAL_META_KEY
from arbora.fleet.gateway_ops import (
    oauth_delegated_call_for_mux,
)
from arbora.fleet.service_child import ServiceChildAdapter
from tests.fleet.catalog_fixture import multiplexer_from_fixture
from tests.fleet.conftest import stdio_fleet_authority  # noqa: F401
from tests.fleet.test_multiplexer_schema_disclosure import SchemaSession
from tests.fleet.test_service_child import (
    REGISTRY_DIGEST,
    Journal,
    _admitted,
    _caller,
    _owner_ref,
)

PLAN_REF = "arbora_plan:" + "ab" * 24
APPROVAL_REF = approval_id_for(PLAN_REF)


class _MetaSession(SchemaSession):
    """Records the ``_meta`` each ``tools/call`` carries."""

    def __init__(self) -> None:
        super().__init__({}, SimpleNamespace(name="drop", meta={}))
        self.metas: list[Any] = []

    async def call_tool(
        self, name: str, arguments: dict[str, Any], meta: Any = None
    ) -> Any:
        self.metas.append(meta)
        return await super().call_tool(name, arguments)


def _mux(tmp_path: Any, monkeypatch: Any, *, oauth: bool) -> tuple[Any, _MetaSession]:
    cfg: dict[str, Any] = {}
    mux = multiplexer_from_fixture(tmp_path / "catalog.json")
    mux._catalog = {"markets": cfg}
    mux._catalog_epoch = 4
    session = _MetaSession()

    async def open_one(_server: str, _cfg: dict[str, Any], _stack: Any) -> Any:
        return session

    monkeypatch.setattr(mux, "_open_one_session", open_one)
    monkeypatch.setattr(mux, "load_catalog", lambda: {"markets": cfg})
    monkeypatch.setattr(
        multiplexer_module,
        "_prepare_runtime_child_policy",
        lambda current: (current, None),
    )
    monkeypatch.setattr(multiplexer_module, "oauth_gated", lambda _cfg: oauth)
    return mux, session


@pytest.mark.spec("ARBORA-OPS-R009.5")
@pytest.mark.usefixtures("stdio_fleet_authority")
@pytest.mark.parametrize("oauth", [False, True], ids=["service", "oauth"])
async def test_multiplexer_paths_set_the_approval_meta(
    tmp_path: Any, monkeypatch: Any, oauth: bool
) -> None:
    mux, session = _mux(tmp_path, monkeypatch, oauth=oauth)
    await mux.call_admitted_child_once("markets", "drop", {}, approval_ref=APPROVAL_REF)
    await mux.call_admitted_child_once("markets", "drop", {})
    assert session.metas == [{APPROVAL_META_KEY: APPROVAL_REF}, None]


@pytest.mark.spec("ARBORA-OPS-R009.5")
async def test_gateway_delegated_call_forwards_the_approval_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import contextlib

    import arbora.security.actor_context as actor_context
    import arbora.security.session as session_module

    monkeypatch.setattr(
        session_module, "use_session", lambda _s: contextlib.nullcontext()
    )
    monkeypatch.setattr(actor_context, "use_actor", lambda _a: contextlib.nullcontext())
    seen: list[dict[str, Any]] = []

    class Mux:
        async def call_admitted_child_once(
            self, server: str, tool: str, arguments: dict, **kwargs: Any
        ) -> Any:
            seen.append(kwargs)
            return SimpleNamespace(is_error=False, model_dump=lambda **_k: {"ok": 1})

    actor = SimpleNamespace(authenticated=True, actor_id="alice", tenant_id="acme")
    caller = SimpleNamespace(
        session=SimpleNamespace(actor=actor), principal="alice", tenant="acme"
    )
    dispatch = oauth_delegated_call_for_mux(Mux())
    await dispatch("s", "t", {}, caller, approval_ref=APPROVAL_REF)
    await dispatch("s", "t", {}, caller)
    assert seen == [{"approval_ref": APPROVAL_REF}, {}]


@pytest.mark.spec("ARBORA-OPS-R009.5")
async def test_gateway_service_call_forwards_the_approval_ref() -> None:
    calls: list[dict[str, Any]] = []

    class Transport:
        async def call_service_child_once(
            self, record: Any, arguments: Any, **kwargs: Any
        ) -> Any:
            calls.append(kwargs)
            return {"ok": True}

    adapter = ServiceChildAdapter(
        journal=Journal(), transport=Transport(), admitted_tool=_admitted
    )
    await adapter(
        "s",
        "run",
        {},
        _caller(),
        _owner_ref(),
        REGISTRY_DIGEST,
        approval_ref=APPROVAL_REF,
    )
    assert calls == [{"approval_ref": APPROVAL_REF}]


@pytest.mark.spec("ARBORA-OPS-R009.5")
async def test_destructive_call_carries_approval_ref() -> None:
    """A plan-confirmed ``fleet.call`` hands the plan's approval to the gateway."""
    got: list[Any] = []

    class Gateway:
        async def call(self, *_args: Any, **kwargs: Any) -> Any:
            got.append(kwargs.get("approval_ref"))
            return {}

    op = next(o for o in fleet.specs() if o.id == "fleet.call")
    params = {"server": "s", "tool": "drop", "arguments": {}}
    for key in (f"plan:{PLAN_REF}", "idem-1", None):
        context = SimpleNamespace(
            caller=SimpleNamespace(),
            services={"fleet_gateway": Gateway()},
            service_identity=False,
            owner="alice",
            fleet_decision=None,
            registry_digest="",
            idempotency_key=key,
        )
        await fleet.handle_fleet_call(context, params, op)
    assert got == [APPROVAL_REF, "", ""]
