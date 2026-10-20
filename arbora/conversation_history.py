"""Read prior conversation messages through native owner-bound checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from arbora.admitted_run import admitted_claim_for, current_admitted_claim
from arbora.api.contracts.session import use_session
from arbora.deployment.semantic_provisioning import CONTROL_GRAPH


class ConversationHistoryUnavailable(RuntimeError):
    """Required history cannot be obtained from its durable owner."""


def _unavailable() -> ConversationHistoryUnavailable:
    return ConversationHistoryUnavailable("durable conversation history is unavailable")


def _owned(row: Any, *, context_id: str, owner_ref: str) -> bool:
    if not isinstance(row, dict) or row.get("kind") != "orchestrator_task":
        return False
    metadata = row.get("metadata")
    return isinstance(metadata, dict) and all(
        metadata.get(key) == value
        for key, value in {
            "a2a_schema": "arbora-a2a-unary-v1",
            "a2a_context_id": context_id,
            "a2a_owner_ref": owner_ref,
        }.items()
    )


def _completed_rows(
    page: Any, binding: Any, context_id: str, owner_ref: str
) -> list[dict[str, Any]]:
    if (
        not isinstance(page, dict)
        or not isinstance(page.get("items"), list)
        or len(page["items"]) > 100
    ):
        raise _unavailable()
    completed = []
    for row in page["items"]:
        if not _owned(row, context_id=context_id, owner_ref=owner_ref):
            raise _unavailable()
        if (
            row.get("work_item_id") == binding.work_item_id
            or row.get("status") != "succeeded"
        ):
            continue
        if (
            not isinstance(row.get("work_item_id"), str)
            or type(row.get("updated_at_ms")) is not int
        ):
            raise _unavailable()
        completed.append(row)
    return completed


def _newest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    latest = max(rows, key=lambda row: row["updated_at_ms"])
    tied = {
        row["work_item_id"]
        for row in rows
        if row["updated_at_ms"] == latest["updated_at_ms"]
    }
    if len(tied) != 1:
        raise _unavailable()
    return latest


async def _latest(
    client: Any, binding: Any, context_id: str, owner_ref: str
) -> dict[str, Any] | None:
    cursor = None
    cursors: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for _ in range(32):
        binding.require_active()
        page = await client.list(
            tenant=binding.session.tenant,
            kind="orchestrator_task",
            limit=100,
            cursor=cursor,
            metadata_match={
                "a2a_schema": "arbora-a2a-unary-v1",
                "a2a_context_id": context_id,
                "a2a_owner_ref": owner_ref,
            },
        )
        binding.require_active()
        candidates.extend(_completed_rows(page, binding, context_id, owner_ref))
        cursor = page.get("next_cursor")
        if cursor is None:
            return _newest(candidates)
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise _unavailable()
        cursors.add(cursor)
    raise _unavailable()


def _project(
    value: Any, row: dict[str, Any], *, context_id: str, owner_ref: str
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise _unavailable()
    current = value.get("work_item")
    if not isinstance(current, dict) or not _owned(
        current, context_id=context_id, owner_ref=owner_ref
    ):
        raise _unavailable()
    run_id = row["metadata"].get("a2a_run_id")
    if (
        not isinstance(run_id, str)
        or not run_id
        or current.get("work_item_id") != row["work_item_id"]
        or current.get("status") != "succeeded"
        or current["metadata"].get("a2a_run_id") != run_id
    ):
        raise _unavailable()
    content = value.get("content")
    if (
        not isinstance(content, str)
        or len(content.encode()) > 1_048_576
        or hashlib.sha256(content.encode()).hexdigest() != value.get("payload_digest")
    ):
        raise _unavailable()
    return {"run_id": run_id, "content": content}


async def read_conversation_history(request: Any) -> dict[str, str] | None:
    """Use only trusted admission fields; request metadata cannot select a context."""
    binding = current_admitted_claim(request)
    if binding is None:
        return None
    required = getattr(binding, "history_required", False)
    context_id = getattr(binding, "context_id", None)
    owner_ref = getattr(binding, "owner_ref", None)
    if not context_id or not owner_ref:
        if required:
            raise _unavailable()
        return None
    try:
        client = binding.engine.graph_compute.for_graph(
            CONTROL_GRAPH
        ).async_client.work_items
        if not callable(getattr(client, "get_checkpoint", None)):
            if required:
                raise _unavailable()
            return None
        with use_session(binding.session):
            row = await asyncio.wait_for(
                _latest(client, binding, context_id, owner_ref), timeout=5.0
            )
            if row is None:
                if required:
                    raise _unavailable()
                return None
            run_id = row["metadata"].get("a2a_run_id")
            if not isinstance(run_id, str) or not run_id:
                raise _unavailable()
            value = await asyncio.wait_for(
                client.get_checkpoint(
                    tenant=binding.session.tenant,
                    work_item_id=row["work_item_id"],
                    run_id=run_id,
                ),
                timeout=5.0,
            )
        binding.require_active()
        return _project(value, row, context_id=context_id, owner_ref=owner_ref)
    except PermissionError as exc:
        raise PermissionError("conversation history authority refused") from exc
    except Exception as exc:
        raise _unavailable() from exc


def require_history_support(run_id: str | None, session: Any) -> None:
    """Refuse canonical continuation before execution on an older harness."""
    binding = admitted_claim_for(run_id, session)
    if binding is None or not binding.history_required:
        return
    from agent_utilities import host_ports

    if not callable(
        getattr(host_ports, "register_run_history_provider", None)
    ) or not callable(getattr(host_ports, "read_run_history", None)):
        raise _unavailable()
