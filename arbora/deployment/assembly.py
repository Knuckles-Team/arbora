"""Assemble the deployment steps the engine chose (ARBORA-DEPLOY-R017.2, .5).

Where to run is the engine's answer (:mod:`arbora.deployment.placement`). The
ordered fleet-tool steps are an agent graph the engine assembles from the
tenant's published deployment template. Arbora sends the request and returns
the engine's graph and certificate unmodified; an abstention passes through
naming the engine's reason.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from typing import Any, Literal

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import OpError
from arbora.api.registry import Confirm, OpSpec
from arbora.deployment.assembly_view import render_graph

#: The template key of one target in the tenant Agent Library.
TEMPLATE_PREFIX = "deployment:"

Send = Callable[[dict[str, Any]], Awaitable[Any]]

UnavailableCause = Literal["decide", "placement_kind", "confirm"]

_UNAVAILABLE: dict[str, tuple[str, str]] = {
    "decide": (
        "ENGINE_DECIDE_UNAVAILABLE",
        "the engine is built without the decide feature",
    ),
    "placement_kind": (
        "PLACEMENT_KIND_UNAVAILABLE",
        "the engine does not serve the placement question kind",
    ),
    "confirm": (
        "CONFIRM_UNAVAILABLE",
        "the plan confirmation service is not bound",
    ),
}


def refuse_unavailable(cause: UnavailableCause) -> OperationRefused:
    """The refusal for one missing producer; each has no side effect."""
    try:
        code, reason = _UNAVAILABLE[cause]
    except KeyError:
        raise ValueError(f"unknown unavailable cause: {cause}") from None
    return OperationRefused("UNAVAILABLE", {"code": code, "reason": reason})


async def assemble_deployment(
    send: Send,
    *,
    tenant: str,
    target: str,
    components: Sequence[str],
) -> Mapping[str, Any]:
    """Send the chosen template and tool components to ``AgentAssemble``.

    ``send`` is the served ``eg.storage.AgentAssemble`` operation bound to the
    caller's engine client. The reply (graph and certificate, or an abstention
    record) is returned as the engine sent it; the request writes nothing.
    """
    request = {
        "tenant_id": tenant,
        "template_key": f"{TEMPLATE_PREFIX}{target}",
        "requirements": {"capabilities": list(components)},
    }
    result = await send({"request": request})
    if not isinstance(result, Mapping):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the engine returned a malformed assembly"}
        )
    return result


def _canonical_digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def graph_digest(graph: Mapping[str, Any] | None) -> str:
    """The plan digest: SHA-256 over the canonical JSON of the engine graph."""
    return _canonical_digest(graph)


def choices_digest(
    target: str, components: Sequence[str], reuse: Collection[str]
) -> str:
    """The digest of the operator's choices: target, components and reused ones."""
    return _canonical_digest(
        {"target": target, "components": sorted(components), "reuse": sorted(reuse)}
    )


def require_confirmed_plan(op: OpSpec) -> None:
    """Refuse ``PLAN_REQUIRED`` unless the operation is gated by plan confirmation.

    The invoke pipeline consumes the plan once before the handler runs and binds
    it to the call's params (``PLAN_REQUIRED``, ``PLAN_EXPIRED`` and
    ``PLAN_MISMATCH`` come from there). This guard keeps the apply handler from
    ever being registered without that gate.
    """
    if op.confirm is not Confirm.PLAN:
        raise OperationRefused("PLAN_REQUIRED", {"operation": op.id})


StepInvoke = Callable[[str, Mapping[str, Any], Any, str], Awaitable[Any]]


async def run_confirmed_steps(
    invoke: StepInvoke,
    caller: Any,
    graph: Mapping[str, Any] | None,
    *,
    plan_digest: str,
    reuse: Collection[str] = (),
    arguments: Mapping[str, Mapping[str, Any]] | None = None,
    key_prefix: str | None = None,
) -> dict[str, Any]:
    """Run the confirmed engine graph, one governed ``fleet.call`` per node.

    The nodes run in engine order under the caller's authority through the
    invoke pipeline (``invoke`` is the bound pipeline). The idempotency key is
    ``deploy:<plan_digest>:<node_id>``, so a replay returns the recorded
    outcome and runs no second effect. The run stops at the first failed node.
    A graph whose digest differs from the confirmed one refuses
    ``PLAN_MISMATCH`` and a node on a reused component refuses
    ``REUSE_COMPONENT_MUTATION``, both before any call. No node is added,
    dropped or reordered.
    """
    if graph_digest(graph) != plan_digest:
        raise OperationRefused("PLAN_MISMATCH", {"reason": "the graph changed"})
    rows = render_graph(graph)
    for row in rows:
        if row["component_id"] in reuse:
            raise OperationRefused(
                "REUSE_COMPONENT_MUTATION",
                {"node_id": row["node_id"], "component_id": row["component_id"]},
            )
        tool = row["tool"]
        if not (
            isinstance(tool, Mapping)
            and isinstance(tool.get("server"), str)
            and isinstance(tool.get("tool"), str)
        ):
            raise OperationRefused("INVALID_ARGUMENT", {"node_id": row["node_id"]})
    steps: list[dict[str, Any]] = []
    ok = True
    for row in rows:
        node_id = row["node_id"]
        tool = row["tool"]
        key = f"{key_prefix or f'deploy:{plan_digest}'}:{node_id}"
        outcome = await invoke(
            "fleet.call",
            {
                "server": tool["server"],
                "tool": tool["tool"],
                "arguments": dict((arguments or {}).get(node_id, {})),
            },
            caller,
            key,
        )
        failed = isinstance(outcome, OpError) or outcome.code != "OK"
        steps.append(
            {
                "node_id": node_id,
                "slot": row["slot"],
                "idempotency_key": key,
                "ok": not failed,
                **({"error": outcome.code} if failed else {}),
            }
        )
        if failed:
            ok = False
            break
    return {"ok": ok, "steps": steps}
