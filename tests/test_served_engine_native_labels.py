"""Released native label readback through the actual ServedEngine producer.

Select ARBORA_NOAUTH_TEST_ENGINE_BINARY explicitly. These isolated storage
checks do not qualify browser admission or authenticated production traffic.
"""

import asyncio
import os
import subprocess
from dataclasses import replace

import pytest

from arbora.access.action_policy import ActionPolicy, ActionRequest
from arbora.api.contracts.session import use_session
from arbora.epistemic import EpistemicClientPool
from arbora.mcp_server.served_engine import ServedEngine
from arbora.security import anonymous
from tests.security.test_anonymous_mode import local_mode as local_mode
from tests.test_real_boot import _free_port, _listening, _terminate, _wait

pytestmark = pytest.mark.spec("ARBORA-FLEET-R015")


@pytest.fixture
def native_engine(tmp_path, local_mode):
    from epistemic_graph import EpistemicGraphClient

    binary = os.environ.get("ARBORA_NOAUTH_TEST_ENGINE_BINARY")
    if not binary:
        pytest.skip("select a released matching native EG binary explicitly")
    port = _free_port()
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "LANG": "C.UTF-8"}
    with (tmp_path / "server.log").open("w") as log:
        process = subprocess.Popen(
            [
                binary,
                "--auth-mode",
                "none",
                "--tcp-only",
                "--tcp-addr",
                f"127.0.0.1:{port}",
                "--persist-dir",
                str(tmp_path / "store"),
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        pool = EpistemicClientPool(
            EpistemicGraphClient.connect, tcp_addr=f"127.0.0.1:{port}"
        )
        try:
            _wait(
                lambda: process.poll() is None and _listening(port),
                timeout=20,
                what="released EG",
            )
            session = replace(anonymous.session(), graph="__commons__")
            with use_session(session):
                yield ServedEngine(pool), session
        finally:
            asyncio.run(pool.close())
            _terminate(process)


def test_actual_create_node_reads_back_by_native_label(native_engine):
    engine, _session = native_engine
    assert engine.create_node("producer-label", "ProducerLabel", {"marker": "native"})
    assert engine.query_cypher("MATCH (n:ProducerLabel) RETURN n.marker AS marker") == [
        {"marker": "native"}
    ]


def test_actual_action_policy_audit_reads_back_in_ledger(native_engine):
    engine, _session = native_engine
    policy = ActionPolicy(engine)
    decision = policy.decide(
        ActionRequest(kind="observe", target="label-probe", source="qualification")
    )
    assert decision.allowed
    rows = policy._recent_decisions("observe")
    assert rows is not None and any(row["id"] == decision.audit_id for row in rows)


def test_actual_add_node_reads_back_by_native_label(native_engine):
    engine, _session = native_engine
    engine.add_node("producer-add-label", "ProducerAddLabel", {"marker": "native"})
    assert engine.query_cypher(
        "MATCH (n:ProducerAddLabel) RETURN n.marker AS marker"
    ) == [{"marker": "native"}]


def test_legacy_type_only_record_is_not_promoted_or_rewritten(native_engine):
    engine, session = native_engine
    client = engine.graph_compute.for_graph(session.graph).async_client
    assert asyncio.run(
        client.nodes.create_if_absent(
            "legacy-type", {"id": "legacy-type", "type": "LegacyLabel", "marker": "old"}
        )
    )
    assert engine.create_node("new-labeled", "LegacyLabel", {"marker": "new"})
    assert engine.query_cypher("MATCH (n:LegacyLabel) RETURN n.marker AS marker") == [
        {"marker": "new"}
    ]
    assert engine.query_cypher(
        "MATCH (n {id: 'legacy-type'}) RETURN n.type AS type, n.node_type AS node_type"
    ) == [{"type": "LegacyLabel", "node_type": None}]
