"""Real EG lease evidence; no background dispatcher or served-task claim."""

from __future__ import annotations

import asyncio
import json
import time
from typing import cast

import pytest
from epistemic_graph import EpistemicGraphClient

from arbora import work_items
from arbora.a2a.tool_task import (
    ToolTaskCarrier,
    WorkItemLeaseLost,
    run_tool_task,
    tool_task_work_item_id,
)
from arbora.a2a.tool_task_dispatch import EgWorkItemFence
from arbora.api.contracts.session import GraphSession, use_session
from arbora.epistemic import ConnectClient, EpistemicClientPool
from arbora.mcp_server.served_engine import ServedEngine
from arbora.security.actor_context import ActorContext, ActorType, CredentialLease
from arbora.security.request_identity import _resolve_authenticated_scopes
from tests.test_real_boot import (
    AUDIENCE,
    POLICY_VERSION,
    PROCESS_SUBJECT,
    ROLES,
    TENANT,
    _booted_stack,
)

pytestmark = [
    pytest.mark.real_boot,
    pytest.mark.spec("ARBORA-A2A-R002.2.2"),
]


def _session() -> GraphSession:
    """Use the process identity admitted by the fixture's signed bootstrap."""
    actor = ActorContext(
        actor_id=PROCESS_SUBJECT,
        actor_type=ActorType.SYSTEM,
        roles=ROLES,
        tenant_id=TENANT,
        authenticated=True,
        credential_lease=CredentialLease(int(time.time()) + 600),
    )
    return GraphSession(
        actor=actor,
        tenant=TENANT,
        scopes=_resolve_authenticated_scopes(actor),
        graph="__control__",
        audience=AUDIENCE,
        policy_version=POLICY_VERSION,
    )


def _submit(engine: ServedEngine, job: str) -> ToolTaskCarrier:
    carrier = ToolTaskCarrier(
        job_id=job,
        payload_ref="tool-task:sha256:" + "a" * 64,
        tenant=TENANT,
        session_id="native-fixture",
        allowed_tool_subset=("query.uql",),
    )
    work_items.submit_work_item_atomic(
        engine,
        kind="tool_task",
        idempotency_key=job,
        work_item_id=tool_task_work_item_id(job),
        tenant=TENANT,
        payload_ref=carrier.payload_ref,
        created_by=PROCESS_SUBJECT,
    )
    return carrier


def _exercise_native_fence(engine: ServedEngine) -> None:
    fence = EgWorkItemFence(engine, tenant=TENANT)
    carrier = _submit(engine, "native-success")
    item = tool_task_work_item_id(carrier.job_id)
    row = fence.get(item)
    assert row is not None and row["payload_ref"] == carrier.payload_ref
    claim = fence.claim(item)
    assert claim is not None
    assert json.loads(claim)["claim"]["attempt"] == 1
    competing = EgWorkItemFence(engine, tenant=TENANT)
    assert competing.claim(item) is None
    fence.require_current(item, claim)
    fence.commit(item, claim, succeeded=True, ref="receipt:native-success")
    row = fence.get(item)
    assert row is not None and row["status"] == "succeeded"
    with pytest.raises(WorkItemLeaseLost):
        fence.require_current(item, claim)
    repeated = run_tool_task(carrier, fence=fence, dispatcher=None, now=time.time())
    assert repeated.outcome == "skipped"
    assert repeated.acknowledge

    refused = _submit(engine, "native-refused")
    outcome = run_tool_task(refused, fence=fence, dispatcher=None, now=time.time())
    assert outcome.outcome == "refused"
    assert outcome.acknowledge
    row = fence.get(tool_task_work_item_id(refused.job_id))
    assert row is not None and row["status"] == "failed"

    uncertain = _submit(engine, "native-uncertain")
    short_lease = EgWorkItemFence(engine, tenant=TENANT, lease_seconds=0.01)
    uncertain_item = tool_task_work_item_id(uncertain.job_id)
    assert short_lease.claim(uncertain_item) is not None
    time.sleep(0.02)
    retry = run_tool_task(uncertain, fence=fence, dispatcher=None, now=time.time())
    assert retry.outcome == "fenced"
    assert not retry.acknowledge
    row = fence.get(uncertain_item)
    assert row is not None and row["status"] == "leased"


def test_native_tool_task_fence_uses_served_engine_methods() -> None:
    with _booted_stack() as stack:
        pool = EpistemicClientPool(
            cast(ConnectClient, EpistemicGraphClient.connect),
            socket_path=stack["socket_path"],
            auth_secret=stack["secret"],
        )
        try:
            with use_session(_session()):
                _exercise_native_fence(ServedEngine(pool))
        finally:
            asyncio.run(pool.close())
