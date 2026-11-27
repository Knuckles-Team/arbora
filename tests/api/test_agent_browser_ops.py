"""Focused contracts for the composed agent and browser operation tables."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from arbora.api.ops import agents, browser
from arbora.api.registry import Effect, PrincipalRule, Registry, Surface


def _context(**services: object) -> SimpleNamespace:
    return SimpleNamespace(
        caller=SimpleNamespace(session=object()),
        client=object(),
        idempotency_key="key-1",
        services=services,
    )


def test_agent_and_browser_ops_have_unique_ids_and_surface_limits() -> None:
    registry = Registry((*agents.operations(), *browser.operations()))
    assert len(registry) == len(
        {op.id for op in (*agents.operations(), *browser.operations())}
    )
    assert registry["agents.run"].effect is Effect.WRITE
    assert (
        registry["browser.execute_call"].principals is PrincipalRule.HUMAN_UNDELEGATED
    )
    assert registry["browser.execute_call"].surfaces == frozenset(
        {Surface.HTTP, Surface.CONSOLE}
    )
    assert all(op.examples for op in registry)


@pytest.mark.usefixtures("default_agent_registered")
@pytest.mark.spec("ARBORA-OPS-R023", "ARBORA-OPS-R002")
@pytest.mark.asyncio
async def test_agent_run_uses_the_existing_a2a_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arbora.security.session

    monkeypatch.setattr(
        arbora.security.session, "use_session", lambda _session: nullcontext()
    )
    calls: list[dict[str, object]] = []

    class Service:
        async def send_message(self, **params: object) -> SimpleNamespace:
            calls.append(params)
            return SimpleNamespace(model_dump=lambda **_kwargs: {"id": "task-1"})

    op = next(op for op in agents.operations() if op.id == "agents.run")
    result = await agents.handle_agent(
        _context(a2a=Service()),
        {"message": {"parts": []}, "context_budget_tokens": None},
        op,
    )
    assert result == {"value": {"id": "task-1"}}
    assert calls == [
        {
            "message": {"parts": []},
            "idempotency_key": "key-1",
            "context_budget_tokens": None,
        }
    ]


@pytest.mark.spec("ARBORA-OPS-R023")
@pytest.mark.asyncio
async def test_browser_op_uses_the_registered_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arbora.security.session

    monkeypatch.setattr(
        arbora.security.session, "use_session", lambda _session: nullcontext()
    )
    called: list[tuple[str, dict[str, object]]] = []

    async def dispatch(
        action: str, payload: dict[str, object], caller: object
    ) -> SimpleNamespace:
        called.append((action, payload))
        return SimpleNamespace(model_dump=lambda **_kwargs: {"status": "accepted"})

    op = next(op for op in browser.operations() if op.id == "browser.execute_call")
    result = await browser.handle_browser(
        _context(browser_control=dispatch), {"payload": {"call_id": "c1"}}, op
    )
    assert result == {"value": {"status": "accepted"}}
    assert called == [("execute_call", {"call_id": "c1"})]


def test_browser_discovery_uses_preloaded_find_without_widening_execution():
    from arbora.api.registry import Effect, Surface, Verb
    from arbora.host_bindings import FLEET_TOOLS

    operations = {op.id: op for op in browser.operations()}
    discovery = operations["browser.discover"]
    assert discovery.verb is Verb.FIND
    assert discovery.verb.value in FLEET_TOOLS
    assert discovery.effect is Effect.READ
    assert Surface.MCP in discovery.surfaces
    assert Surface.MCP not in operations["browser.execute_call"].surfaces


class _Grant(SimpleNamespace):
    pass


class _Grants:
    def __init__(self, grants: dict[str, _Grant]) -> None:
        self._grants = grants

    async def get(self, grant_id: str) -> _Grant | None:
        return self._grants.get(grant_id)


class _Provider:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []

    def perform(self, action: object, grant: object) -> SimpleNamespace:
        self.calls.append((action, grant))
        return SimpleNamespace(
            ok=True, evidence="clicked", captured_cursor=False, captured_focus=False
        )


def _desktop_context(grants: dict[str, _Grant], provider: _Provider) -> SimpleNamespace:
    return SimpleNamespace(
        caller=SimpleNamespace(session=object(), tenant="tenant-a"),
        client=object(),
        idempotency_key="key-1",
        services={"desktop_grants": _Grants(grants), "desktop_provider": provider},
    )


def _desktop_params(grant_id: str) -> dict[str, object]:
    return {"grant_id": grant_id, "kind": "click", "target": "ok", "text": ""}


def _install_desktop_double(monkeypatch: pytest.MonkeyPatch) -> None:
    """A structural double of ``scion_sdk.desktop`` for a pinned SDK that lacks it."""
    import enum
    import sys
    from dataclasses import dataclass
    from types import ModuleType

    class Kind(enum.StrEnum):
        CLICK = "click"
        TYPE = "type"
        VERIFY = "verify"

    @dataclass(frozen=True)
    class Action:
        kind: Kind
        target: str
        text: str = ""

    @dataclass(frozen=True)
    class Grant:
        targets: tuple[str, ...]

    module = ModuleType("scion_sdk.desktop")
    module.DesktopAction = Action  # type: ignore[attr-defined]
    module.DesktopActionKind = Kind  # type: ignore[attr-defined]
    module.DesktopGrant = Grant  # type: ignore[attr-defined]
    module.DesktopProvider = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scion_sdk.desktop", module)


@pytest.mark.spec("ARBORA-HOST-R029")
@pytest.mark.asyncio
async def test_native_desktop_actions_require_verified_grant_without_focus_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.invoke.pipeline import OperationRefused

    _install_desktop_double(monkeypatch)
    provider = _Provider()
    grants = {
        "ok": _Grant(tenant="tenant-a", revoked=False, targets=("ok",)),
        "revoked": _Grant(tenant="tenant-a", revoked=True, targets=("ok",)),
        "foreign": _Grant(tenant="tenant-b", revoked=False, targets=("ok",)),
    }
    op = next(o for o in browser.operations() if o.id == "browser.desktop_action")
    context = _desktop_context(grants, provider)

    for grant_id, code in (
        ("absent", "DESKTOP_GRANT_MISSING"),
        ("revoked", "DESKTOP_GRANT_REVOKED"),
        ("foreign", "DESKTOP_GRANT_CROSS_TENANT"),
    ):
        with pytest.raises(OperationRefused) as refused:
            await browser.handle_desktop_action(context, _desktop_params(grant_id), op)
        assert refused.value.code == code
    assert provider.calls == []

    result = await browser.handle_desktop_action(context, _desktop_params("ok"), op)
    assert result["value"] == {"ok": True, "evidence": "clicked", "grant_id": "ok"}
    assert len(provider.calls) == 1


@pytest.mark.spec("ARBORA-HOST-R029")
@pytest.mark.asyncio
async def test_desktop_action_stays_unavailable_while_the_sdk_port_is_unpublished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from arbora.api.invoke.pipeline import OperationRefused

    monkeypatch.setitem(sys.modules, "scion_sdk.desktop", None)
    provider = _Provider()
    grants = {"ok": _Grant(tenant="tenant-a", revoked=False, targets=("ok",))}
    op = next(o for o in browser.operations() if o.id == "browser.desktop_action")
    with pytest.raises(OperationRefused) as refused:
        await browser.handle_desktop_action(
            _desktop_context(grants, provider), _desktop_params("ok"), op
        )
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "scion_sdk.desktop.DesktopProvider"
    assert provider.calls == []


class _States:
    def __init__(self, refusal: str | None = None) -> None:
        self.seen: list[dict[str, object]] = []
        self._refusal = refusal

    async def resolve(self, **kwargs: object) -> SimpleNamespace:
        self.seen.append(kwargs)
        return SimpleNamespace(refusal=self._refusal, state={"frame": kwargs["frame"]})


def _visual_context(states: object | None) -> SimpleNamespace:
    services = {} if states is None else {"visual_state": states}
    return SimpleNamespace(
        caller=SimpleNamespace(session=object(), tenant="tenant-a"),
        services=services,
    )


_OBSERVATION = {"run_id": "run-1", "source": "tab-1", "frame": "f1", "revision": 3}


@pytest.mark.spec("ARBORA-HOST-R030")
@pytest.mark.asyncio
async def test_visual_state_is_run_source_and_tenant_bound() -> None:
    from arbora.api.invoke.pipeline import OperationRefused

    observe = next(o for o in browser.operations() if o.id == "browser.observe")
    act = next(o for o in browser.operations() if o.id == "browser.act")
    states = _States()
    first = await browser.handle_visual_state(
        _visual_context(states), _OBSERVATION, observe
    )
    second = await browser.handle_visual_state(
        _visual_context(states), _OBSERVATION, act
    )
    assert first["value"]["provenance"] == {
        "source": "tab-1",
        "frame": "f1",
        "revision": 3,
    }
    assert second["value"]["state"] == {"frame": "f1"}
    # Run, tenant and the observation reach the engine; nothing is kept here.
    assert states.seen[0] == {
        "action": "observe",
        "tenant": "tenant-a",
        "run_id": "run-1",
        "source": "tab-1",
        "frame": "f1",
        "revision": 3,
    }
    assert states.seen[1]["action"] == "act"
    for code in ("VISUAL_STATE_STALE", "VISUAL_STATE_CROSS_TENANT"):
        with pytest.raises(OperationRefused) as refused:
            await browser.handle_visual_state(
                _visual_context(_States(refusal=code)), _OBSERVATION, act
            )
        assert refused.value.code == code


@pytest.mark.spec("ARBORA-HOST-R030")
@pytest.mark.asyncio
async def test_visual_state_is_unavailable_until_the_engine_serves_it() -> None:
    from arbora.api.errors import ArboraRefusal

    observe = next(o for o in browser.operations() if o.id == "browser.observe")
    with pytest.raises(ArboraRefusal) as refused:
        await browser.handle_visual_state(_visual_context(None), _OBSERVATION, observe)
    assert refused.value.details["spec_row"] == "MEDULLA-DECISION-ENGINE-R816"
