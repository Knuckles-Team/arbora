"""The worker that runs one ``workflow.run`` WorkItem (ARBORA-OPS-R913).

``workflows.run`` submits a WorkItem; the engine's WorkItem machine holds the
run state and calls no connector. This worker claims the item, calls the
business-process connector's ``run`` tool through ``fleet.call`` as the
verified session, and commits the terminal state through
``CommitWorkItemResult``. While the fleet does not serve that tool the worker
refuses ``UNAVAILABLE`` before it claims, so the item stays queued and no run
is reported as started that was not.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from arbora import work_items
from arbora.api.invoke.pipeline import OperationRefused
from arbora.modality.fleet_port import FleetCallFailed, FleetPort

SERVER = "business-process"
TOOL = "run"
KIND = "workflow.run"
LEASE_TTL_S = 300.0
_DISCOVERY = "business process workflow run"


class WorkflowWorker:
    """Claim one workflow run, call its connector, commit the outcome."""

    def __init__(self, engine: Any, fleet: FleetPort, *, worker_ref: str) -> None:
        self._engine = engine
        self._fleet = fleet
        self._worker_ref = worker_ref

    async def _served(self) -> bool:
        try:
            items = await self._fleet.search(_DISCOVERY)
        except FleetCallFailed:
            return False
        return any(
            item.get("server") == SERVER and item.get("name") == TOOL for item in items
        )

    async def run(self, item_id: str) -> str:
        """Run one item under the current verified session; return its outcome.

        ``committed`` and ``noop`` are the work-item commit results; a run
        another worker holds answers ``not_claimed``.
        """
        if not await self._served():
            raise OperationRefused(
                "UNAVAILABLE",
                {"reason": f"the fleet does not serve {SERVER} {TOOL}"},
            )
        item = work_items.get_work_item(self._engine, item_id)
        if item is None or item.get("kind") != KIND:
            raise OperationRefused("NOT_FOUND")
        if item.get("status") in work_items.TERMINAL_WORK_ITEM_STATUSES:
            return "noop"
        claim = work_items.claim_specific(
            self._engine, item_id, token=self._worker_ref, lease_ttl_s=LEASE_TTL_S
        )
        if claim is None:
            return "not_claimed"
        metadata: Mapping[str, Any] = item.get("metadata") or {}
        arguments = {
            "workflow_id": metadata.get("workflow_id"),
            "inputs": dict(metadata.get("inputs") or {}),
        }
        try:
            reply = await self._fleet.call(SERVER, TOOL, arguments)
        except FleetCallFailed as failure:
            return work_items.commit_result(
                self._engine,
                item_id,
                claim,
                outcome="failed",
                error_ref=f"fleet:{failure.code}",
                retryable=failure.code == "UNAVAILABLE",
            )
        digest = hashlib.sha256(
            json.dumps(reply, sort_keys=True, default=str).encode()
        ).hexdigest()
        return work_items.commit_result(
            self._engine,
            item_id,
            claim,
            outcome="succeeded",
            result_ref=f"sha256:{digest}",
        )


class WorkflowRunStarter:
    """Start the worker for a freshly submitted run, under the submitter's session.

    The task holds no state of its own: the WorkItem is the run, so a task lost
    with its process leaves the item to the next claim (the lease expires).
    """

    def __init__(self, get_engine: Any, fleet: FleetPort, *, worker_ref: str) -> None:
        self._get_engine = get_engine
        self._fleet = fleet
        self._worker_ref = worker_ref
        self._tasks: set[Any] = set()

    def start(self, item_id: str, session: Any) -> None:
        import asyncio

        task = asyncio.ensure_future(self._run(item_id, session))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, item_id: str, session: Any) -> None:
        from arbora.security.session import use_session

        worker = WorkflowWorker(
            self._get_engine(), self._fleet, worker_ref=self._worker_ref
        )
        try:
            with use_session(session):
                await worker.run(item_id)
        except OperationRefused:
            return  # the item stays queued until the fleet serves the tool


__all__ = ["KIND", "SERVER", "TOOL", "WorkflowRunStarter", "WorkflowWorker"]
