"""Every ``Decide`` arbora sends carries the engine's ``{request: ...}`` wire params.

The engine's ``MethodDecideParams`` is ``{request}`` with unknown fields forbidden, so a
bare request is refused. One helper, ``arbora.api.domain.deployment.placement.send_decide``, builds the
params; this proves the sites that sent a bare request now wrap, and that no ``Decide`` is
sent anywhere in the package without the wrapper.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.domain.deployment.placement import decide_params, send_decide

ROOT = Path(__file__).resolve().parents[2] / "arbora"


class _Stop(Exception):
    """Raised by the capturing sender once it has seen the params."""


def _capture() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def send(method: str, params: dict[str, Any], *tail: Any) -> Any:
        seen.append((method, params))
        raise _Stop

    return seen, send


@pytest.mark.spec("ARBORA-A2A-R005.4")
async def test_the_admission_question_is_sent_wrapped() -> None:
    from arbora.a2a.engine_decide import build_ask_decide

    seen, send = _capture()
    ask = build_ask_decide(
        send,
        graph="graph:one",
        feature_schema=lambda tenant: {"component_id": "pin", "digest": "d"},
        principal=lambda: "principal:one",
    )
    with pytest.raises(_Stop):
        await ask(
            "held_item_admission",
            {"item_kind": "k", "item_ref": "r", "content_digest": "c"},
            "tenant:one",
        )
    [(method, params)] = seen
    assert method == "Decide" and list(params) == ["request"]
    assert params["request"]["tenant_id"] == "tenant:one"


@pytest.mark.spec("ARBORA-A2A-R005.4")
async def test_the_host_decision_port_wraps_the_request() -> None:
    from arbora.a2a.engine_decide import HostDecide

    seen, send = _capture()
    port = HostDecide()
    port.bind(send, feature_schema=lambda t: {}, principal=lambda: "p")
    with pytest.raises(_Stop):
        await port({"tenant_id": "tenant:one", "question": {"kind": "placement"}})
    assert seen == [
        (
            "Decide",
            {"request": {"tenant_id": "tenant:one", "question": {"kind": "placement"}}},
        )
    ]


@pytest.mark.spec("ARBORA-OPS-R877")
async def test_the_team_question_is_sent_wrapped() -> None:
    from arbora.api.domain.a2a.team import TeamCoordinator

    seen, base = _capture()

    async def send(method: str, params: dict[str, Any]) -> Any:
        if method != "Decide":
            return None
        return await base(method, params)

    coordinator = TeamCoordinator(
        send,
        "tenant:one",
        graph="graph:one",
        feature_schema={"component_id": "pin", "digest": "d"},
        principal="principal:one",
    )
    with pytest.raises(_Stop):
        await coordinator.coordinate(
            team_id="t",
            members=["a"],
            tasks={"x": "working"},
            channel_signal="available",
            observed_at_ms=1,
        )
    [(method, params)] = seen
    assert method == "Decide" and list(params) == ["request"]


@pytest.mark.spec("ARBORA-FLEET-R083.2")
async def test_the_tool_ranking_question_is_sent_wrapped() -> None:
    from arbora.fleet.tool_ranking import engine_tool_order

    seen, send = _capture()
    order = engine_tool_order(
        send,
        feature_schema=lambda tenant: {"component_id": "pin", "digest": "d"},
        principal=lambda: "principal:one",
    )
    with pytest.raises(_Stop):
        await order(SimpleNamespace(tenant="tenant:one"), query="find a tool")
    [(method, params)] = seen
    assert method == "Decide" and list(params) == ["request"]


@pytest.mark.spec("ARBORA-A2A-R005.4")
async def test_send_decide_wraps_once_and_keeps_the_tail() -> None:
    got: list[Any] = []

    async def send(*args: Any) -> str:
        got.append(args)
        return "ok"

    request = {"tenant_id": "t"}
    assert await send_decide(send, request, "t") == "ok"
    assert got == [("Decide", {"request": request}, "t")]
    assert decide_params({"a": 1}) == {"request": {"a": 1}}


def _decide_params_of(node: ast.Call) -> ast.expr | None:
    """The params argument of a call that sends ``"Decide"``, or ``None``."""
    args = node.args
    at = next((i for i, a in enumerate(args) if _is_decide(a)), None)
    if at is None or at + 1 >= len(args):
        return None
    return args[at + 1]


def _bare_decide_sites() -> list[str]:
    """Every call that sends ``"Decide"`` whose params are not a ``{request}`` wrap."""
    calls = (
        (path, node)
        for path in sorted(ROOT.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
    )
    found = ((path, node, _decide_params_of(node)) for path, node in calls)
    return [
        f"{path.relative_to(ROOT)}:{node.lineno}"
        for path, node, params in found
        if params is not None and not _wrapped(params)
    ]


def _is_decide(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value == "Decide"


def _wrapped(node: ast.expr) -> bool:
    if isinstance(node, ast.Dict):
        return [getattr(k, "value", None) for k in node.keys] == ["request"]
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        return name == "decide_params"
    return False


@pytest.mark.spec("ARBORA-A2A-R005.4")
def test_no_decide_is_sent_without_the_request_wrapper() -> None:
    assert _bare_decide_sites() == []
