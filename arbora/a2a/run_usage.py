"""Shared turn measurements from the native, caller-owned completion receipt."""

from __future__ import annotations

import json
from typing import Any

from arbora.api.contracts.agent_control_contracts import AgentExecutionResult


def project_run_usage(
    value: Any,
    *,
    session: Any,
    task: Any,
    item: dict[str, Any],
    owner_ref: str,
    owner_graph: str,
) -> dict[str, Any]:
    """Validate native admission and terminal binding before exposing counters."""
    from epistemic_graph.generated.models import RunWorkItemView

    unavailable = {"status": "unavailable", "reason": "producer_unavailable"}
    if value is None:
        return unavailable
    view = RunWorkItemView.model_validate_json(json.dumps(value), strict=True)
    admission = view.admission.model_dump(mode="json")
    current = view.work_item.model_dump(mode="json")
    metadata = admission["metadata"]
    expected = item["metadata"]
    run_id = expected["a2a_run_id"]
    if not (
        admission["run_id"] == run_id
        and admission["context"]["tenant_id"] == session.tenant
        and admission["context"]["graph"] == owner_graph
        and current["work_item_id"] == item["work_item_id"]
        and current["status"] == item["status"]
        and current["kind"] == "orchestrator_task"
        and metadata.get("a2a_owner_ref") == owner_ref
        and all(
            metadata.get(key) == expected.get(key) == current["metadata"].get(key)
            for key in ("a2a_schema", "a2a_run_id", "a2a_context_id", "a2a_owner_ref")
        )
        and metadata.get("a2a_schema") == "arbora-a2a-unary-v1"
        and metadata.get("a2a_context_id") == task.context_id
    ):
        raise PermissionError("native run completion crossed its admission scope")
    receipt = view.run_completion
    if receipt is None:
        return unavailable
    payload = receipt.payload
    if not (
        payload.tenant == session.tenant
        and payload.work_item_id == item["work_item_id"]
        and payload.completion.run_id == run_id
        and payload.completion.schema_version == 1
        and payload.outcome == item["status"]
        and payload.outcome in {"succeeded", "failed", "cancelled"}
        and (payload.outcome == "succeeded" or payload.result_ref is None)
    ):
        raise ValueError("native run completion does not match the terminal task")
    if payload.completion.usage is None:
        return unavailable
    counts = payload.completion.usage.model_dump(mode="json", exclude_none=True)
    validated = AgentExecutionResult.model_validate(
        {"run_id": run_id, "output": "", "usage": counts}
    )
    return {
        "runId": run_id,
        "taskId": task.id,
        "contextId": task.context_id,
        "scope": "turn",
        "counts": validated.usage,
    }


async def read_run_usage(
    engine: Any,
    *,
    session: Any,
    task: Any,
    item: dict[str, Any],
    owner_ref: str,
    owner_graph: str,
) -> dict[str, Any]:
    """Use the existing owner graph's caller-bound GetRunWorkItem surface."""
    client = engine.graph_compute.for_graph(owner_graph).async_client
    value = await client.work_items.get_run(
        tenant=session.tenant, run_id=item["metadata"]["a2a_run_id"]
    )
    return project_run_usage(
        value,
        session=session,
        task=task,
        item=item,
        owner_ref=owner_ref,
        owner_graph=owner_graph,
    )
