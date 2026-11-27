"""A delivery runs from slice to merge over MCP (ARBORA-OPS-R896.13).

One journey over the real served surface with a loopback engine and recorded code-host
events: MCP ``act`` ``development.run.start`` opens the delivery and starts its assignment on a
reserved lane; signed code-host events reach ``DeliveryFeedback`` through the identity
middleware; the engine's ``disarm`` and ``arm`` verdicts reach the connector arm tools with an
owner receipt on the new head; a merge event ends the delivery. The platform principal never
merges.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.ops import development_delivery as delivery
from arbora.api.ops import development_scm as scm_intake
from arbora.identity.signed_ingress import ScmSignedIngress, scm_sources
from arbora.security.signed_ingress import bind_signed_ingress
from tests.api._served_support import McpAsk, person, route_methods
from tests.api.test_delivery_ops_served import _arming_record, _Connector, _Receipts
from tests.api.test_development_surface_served import (  # noqa: F401
    runs_env,
    stack,
)
from tests.development.test_assignments_served import Lanes, Runner
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

PRINCIPAL = "person:one"


def _event(event_id: str, kind: str, **extra: Any) -> dict[str, Any]:
    return {**EVENT, "event_id": event_id, "kind": kind, **extra}


@pytest.mark.spec("ARBORA-OPS-R896.13")
def test_delivery_runs_from_slice_to_merge_over_mcp(
    runs_env: Any,  # noqa: F811
) -> None:
    app, engine, runs, invocation, monkeypatch = runs_env
    engine.modes["repo"] = "development"
    lanes, runner = Lanes(active=0), Runner()
    monkeypatch.setattr(
        delivery,
        "_assignment_ports",
        lambda context: (lanes, runner, 4, lambda: NOW_MS),
    )
    monkeypatch.setenv("ARBORA_DEVELOPMENT_SCM_SOURCES", SOURCES)
    held: set[str] = set()
    audit: list[dict[str, str]] = []
    bind_signed_ingress(
        ScmSignedIngress(
            scm_sources(SOURCES),
            lambda _s: MemoryReplay(held),
            clock_ms=lambda: NOW_MS,
            resolve=lambda _r: SECRET,
            audit=audit.append,
        )
    )
    monkeypatch.setattr(scm_intake, "_system_clock_ms", lambda: NOW_MS)
    fed: list[dict[str, Any]] = []
    incoming: list[dict[str, Any]] = []

    def open_delivery(params: dict[str, Any], who: str) -> Any:
        runs.calls.append(("DeliveryOpen", params))
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
        assert context.caller.principal == "svc:scm-github"
        return incoming.pop(0)

    monkeypatch.setattr(scm_intake, "_call_connector", normalize)

    # 1. MCP act: the run opens a delivery and its assignment starts on a reserved lane.
    ask = McpAsk(
        invocation, monkeypatch, person("one", extra_scopes=("lifecycle:write",))
    )
    started = ask(
        "act",
        "development.run.start",
        {"repository": "repo", "phase": "phase-1"},
        idempotency_key="j1",
    )
    assert started["result"] == {"run_id": "run:mine"}, started
    assert lanes.reserved[0]["work_item_id"] == "delivery:run:mine:a1"
    assert runner.started[0][0]["delivery_id"] == "run:mine"

    # 2. A failed check, then a pushed revision, then a merge event, each signed.
    for number, event in enumerate(
        (
            _event("e-check", "check_changed", failed=True, summary="lint failed"),
            _event("e-push", "head_pushed"),
            _event("e-merge", "proposal_merged", actor_kind="bot"),
        )
    ):
        incoming.append(event)
        assert _post(app, BODY, delivery=f"dl-{number}").status_code == 200
    assert [(e["kind"], e["failed"]) for e in fed] == [
        ("check_changed", True),
        ("head_pushed", False),
        ("proposal_merged", False),
    ]
    assert fed[2]["source"] == "automation"

    # 3. The engine's verdicts reach the connector: disarm on the new head, arm with a receipt.
    def arming(verdict: str) -> tuple[Any, _Connector]:
        connector = _Connector()
        context = SimpleNamespace(
            caller=SimpleNamespace(tenant="tenant:one", principal=PRINCIPAL),
            services={
                "owner_receipts": _Receipts(),
                "delivery_connector": connector,
                "task_plan_feature_schema": {"feature_schema": "pin"},
            },
            idempotency_key="k",
        )

        async def send(ctx: Any, method: str, params: dict[str, Any], **_k: Any) -> Any:
            return {"records": [_arming_record(verdict, params["request"])]}

        monkeypatch.setattr(delivery, "engine_send", send)
        return context, connector

    params = {
        "delivery_id": "run:mine",
        "proposal": "github:org/repo#7",
        "head_sha": "abc1234",
        "approval_ref": "receipt:owner",
    }
    off_context, off = arming("disarm")
    asyncio.run(delivery.handle_delivery_arm(off_context, params, None))  # type: ignore[arg-type]
    on_context, on = arming("arm")
    asyncio.run(delivery.handle_delivery_arm(on_context, params, None))  # type: ignore[arg-type]
    assert [tool for tool, _ in off.calls] == ["disarm_merge"]
    assert [tool for tool, _ in on.calls] == ["arm_merge"]
    # Zero merge calls by the platform principal: only the arm and disarm tools were called.
    assert all(
        "merge" in tool and tool.endswith("_merge") for tool, _ in off.calls + on.calls
    )
    assert not any(tool == "merge" for tool, _ in off.calls + on.calls)
    bind_signed_ingress(None)
