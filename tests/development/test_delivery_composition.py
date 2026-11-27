"""The delivery ports are composed in the production host (ARBORA-OPS-R896.9, R896.12).

The served invocation of the host binds the lane authority, the run starter, the identity
links, the source access, the owner receipts and the code-host connector. These tests drive
the real served surface with NO test double for the ports themselves: the doubles sit at the
engine boundary (the sync lane client, the engine answers behind the pin-lag guard) and at
the invoke pipeline's ``agents.run`` start. ``run.start`` reaches the reserved lane and a
signed event is stamped with the production bindings.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import OpError, OpResult
from arbora.api.ops import delivery_ports
from arbora.api.ops import development_scm as scm_intake
from arbora.api.ops.plans import LateStepInvoke
from arbora.identity.signed_ingress import ScmSignedIngress, scm_sources
from arbora.security.signed_ingress import bind_signed_ingress
from tests.api._served_support import McpAsk, person, route_methods
from tests.api.test_development_surface_served import (  # noqa: F401
    runs_env,
    stack,
)
from tests.development.test_scm_intake_served import (
    BODY,
    EVENT,
    NOW_MS,
    SECRET,
    SOURCES,
    MemoryReplay,
    _post,
)
from tests.test_served_operation_api import served  # noqa: F401


class LaneClient:
    """The sync generated engine client: a ``development_lanes`` namespace and ``supports``."""

    def __init__(self) -> None:
        self.development_lanes = self
        self.reserved: list[dict[str, Any]] = []

    def supports(self, name: str) -> bool:
        return True

    def status(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"tenant_active_count": 0, "holds": []}

    def reserve(self, request: dict[str, Any]) -> dict[str, Any]:
        self.reserved.append(request)
        return {"hold_id": "hold:1"}


@pytest.mark.spec("ARBORA-OPS-R896.9")
@pytest.mark.spec("ARBORA-OPS-R896.12")
def test_run_start_reaches_the_lane_and_a_signed_event_is_stamped_with_production_bindings(
    runs_env: Any,  # noqa: F811
) -> None:
    app, engine, runs, invocation, monkeypatch = runs_env
    engine.modes["repo"] = "development"
    lane = LaneClient()
    monkeypatch.setattr(delivery_ports, "_lane_client", lambda graph: lane)
    started: list[tuple[str, dict[str, Any], str]] = []

    async def agents_run(
        self: Any, op_id: str, params: dict[str, Any], caller: Any, key: str
    ) -> Any:
        started.append((op_id, params, key))
        return OpResult(value={"value": {"id": "task:1"}})

    monkeypatch.setattr(LateStepInvoke, "__call__", agents_run)
    # The ports are the production ones: the host composition bound them.
    bound = invocation.services.runtime.bindings
    assert all(
        isinstance(bound[name], delivery_ports.ContextPort)
        for name in (
            "delivery_lanes",
            "delivery_run_starter",
            "identity_links",
            "source_access",
            "owner_receipts",
            "delivery_connector",
        )
    )
    # The engine's identity link and path grants answer behind the pin-lag guard.
    asked: list[tuple[str, dict[str, Any]]] = []

    async def engine_answer(
        context: Any, method: str, params: dict[str, Any], **_k: Any
    ) -> Any:
        asked.append((method, params))
        if method == "Identity":
            assert params == {
                "op": {
                    "family": "idp",
                    "op": "resolve_link",
                    "request": {"idp_id": "github", "subject": "octocat"},
                }
            }
            return {"kind": "linked_principal", "value": {"principal_id": "person:one"}}
        return {"grants": [{"repo": "org/repo", "prefix": "", "write": True}]}

    monkeypatch.setattr("arbora.api.ops.pin_guards.guard", lambda *a, **k: None)
    monkeypatch.setattr("arbora.api.ops.pin_guards.engine_send", engine_answer)
    monkeypatch.setenv("ARBORA_DEVELOPMENT_SCM_SOURCES", SOURCES)
    bind_signed_ingress(
        ScmSignedIngress(
            scm_sources(SOURCES),
            lambda _s: MemoryReplay(set()),
            clock_ms=lambda: NOW_MS,
            resolve=lambda _r: SECRET,
            audit=lambda _row: None,
        )
    )
    monkeypatch.setattr(scm_intake, "_system_clock_ms", lambda: NOW_MS)
    fed: list[dict[str, Any]] = []

    def open_delivery(params: dict[str, Any], who: str) -> Any:
        return {
            "delivery": {
                "id": "run:mine",
                "scope_rows": ["T001"],
                "assignments": [{"id": "a1", "inputs": []}],
            },
            "would_dispatch": None,
            "reopened": False,
        }

    def feedback(params: dict[str, Any], _who: str) -> Any:
        fed.append(params["event"])
        return {
            "duplicate": False,
            "delivered_to": ["run:mine"],
            "resumed": [],
            "defect_filed": False,
        }

    route_methods(
        engine,
        {"DeliveryOpen": open_delivery, "DeliveryFeedback": feedback},
        monkeypatch,
        {"fixture-verified": person("one", extra_scopes=("lifecycle:write",))},
    )

    async def normalize(
        context: Any, server: str, tool: str, args: dict[str, Any]
    ) -> Any:
        return EVENT

    monkeypatch.setattr(scm_intake, "_call_connector", normalize)
    ask = McpAsk(
        invocation, monkeypatch, person("one", extra_scopes=("lifecycle:write",))
    )
    answer = ask(
        "act",
        "development.run.start",
        {"repository": "repo", "phase": "phase-1"},
        idempotency_key="c1",
    )
    assert answer["result"] == {"run_id": "run:mine"}, answer
    # The lane was reserved on the engine, and one run started on that hold.
    assert lane.reserved[0]["work_item_id"] == "delivery:run:mine:a1"
    (op_id, params, key), *rest = started
    assert op_id == "agents.run" and not rest
    assert key == "delivery-run:run:mine:hold:1"
    metadata = params["message"]["metadata"]
    assert metadata["lane_hold"] == {"hold_id": "hold:1"}
    assert metadata["delivery_assignment"]["delivery_id"] == "run:mine"
    # A signed event is stamped with the principal the engine's link and grant resolve.
    assert _post(app, BODY).status_code == 200
    assert fed[0]["admitted_principal"] == "person:one"
    assert [method for method, _ in asked] == ["Identity", "ListPathGrants"]
    bind_signed_ingress(None)


@pytest.mark.spec("ARBORA-OPS-R896.12")
def test_unserved_engine_shapes_queue_the_assignment_and_stamp_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.ops.development_assignments import start_assignment

    context = SimpleNamespace(
        caller=SimpleNamespace(session=SimpleNamespace(graph="g1")),
        services=delivery_ports.delivery_bindings(),
    )
    # A release whose client lacks the lane namespace: the assignment waits.
    monkeypatch.setattr(delivery_ports, "_lane_client", lambda graph: SimpleNamespace())
    lanes = delivery_ports.delivery_port(context, "delivery_lanes")
    runner = SimpleNamespace(start=None)
    queued = asyncio.run(
        start_assignment(
            {"id": "d1"},
            {"id": "a1"},
            tenant="t",
            lanes=lanes,
            runner=runner,
            now_ms=lambda: 1,
        )
    )
    assert queued == {
        "state": "queued",
        "reason": "native_development_lane_authority_unavailable",
    }
    # The pinned release lacks the identity-link and path-grant shapes: nobody is stamped.
    links = delivery_ports.delivery_port(
        SimpleNamespace(services=delivery_ports.delivery_bindings()), "identity_links"
    )
    assert asyncio.run(links.principal_for("github", "octocat")) is None


@pytest.mark.spec("ARBORA-OPS-R896.12")
def test_a_refused_run_start_queues_after_the_lane_is_held() -> None:
    from arbora.api.ops.development_assignments import start_assignment

    class Held:
        def status(self, request: dict[str, Any]) -> dict[str, Any]:
            return {"tenant_active_count": 0}

        def reserve(self, request: dict[str, Any]) -> dict[str, Any]:
            return {"hold_id": "hold:1"}

    async def refuse(*_a: Any) -> Any:
        return OpError("SCOPE_DENIED")

    starter = delivery_ports.InvokeRunStarter(refuse, SimpleNamespace())
    queued = asyncio.run(
        start_assignment(
            {"id": "d1"},
            {"id": "a1"},
            tenant="t",
            lanes=Held(),
            runner=starter,
            now_ms=lambda: 1,
        )
    )
    assert queued == {"state": "queued", "reason": "SCOPE_DENIED"}
    with pytest.raises(OperationRefused):
        asyncio.run(starter.start({"delivery_id": "d1"}, {"hold_id": "h"}))
