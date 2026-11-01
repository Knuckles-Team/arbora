"""Actual native bytes and locator reads; no queue or caller recovery claim."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from medulla import MedullaClient

from arbora.a2a.tool_task import ToolTaskRequest
from arbora.a2a.tool_task_requests import EgToolTaskRequestStore
from arbora.api.contracts.session import use_session
from arbora.epistemic import ConnectClient, EpistemicClientPool
from arbora.mcp_server.served_engine import ServedEngine
from tests.a2a.test_tool_task_native import _session
from tests.test_real_boot import PROCESS_SUBJECT, TENANT, _booted_stack

pytestmark = [
    pytest.mark.real_boot,
    pytest.mark.spec("ARBORA-A2A-R002.2.3.1"),
]


def test_native_request_survives_durable_engine_reopen() -> None:
    request = ToolTaskRequest(
        operation_id="query.uql",
        parameters_json='{"query":"MATCH (n) RETURN n LIMIT 1"}',
        principal=PROCESS_SUBJECT,
        tenant=TENANT,
        session_id="native-request-session",
        authority_ref="native-request-original-reference",
        policy_revision="native-request-policy",
        registry_digest="c" * 64,
    )
    with _booted_stack() as stack:
        for index in range(2):
            if index == 1:
                stack["restart_engine"]()
            pool = EpistemicClientPool(
                cast(ConnectClient, MedullaClient.connect),
                socket_path=stack["socket_path"],
                auth_secret=stack["secret"],
            )
            try:
                with use_session(_session()):
                    store = EgToolTaskRequestStore(ServedEngine(pool))
                    if index == 0:
                        carrier = store.store("native-request-job", request)
                        assert carrier.payload_ref == request.request_ref
                    resolved = store.resolve(
                        request.request_ref,
                        job_id="native-request-job",
                        tenant=TENANT,
                        session_id=request.session_id,
                    )
                    assert resolved == request
            finally:
                asyncio.run(pool.close())
