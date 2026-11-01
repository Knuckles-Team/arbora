"""Schema receipt wiring with the real policy owner and a recorded audit store.

The policy file and graph backend are explicit test fixtures. These checks do
not authorize native storage or qualify the separate approval-grant flow.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("medulla.decision_stat")

from arbora.access import action_policy  # noqa: E402
from arbora.api import task_plan_schema  # noqa: E402
from arbora.api.contracts.session import (  # noqa: E402
    GraphSession,
    SessionExpiredError,
    current_session,
    resolve_session,
    use_session,
)
from arbora.epistemic import ClientContext  # noqa: E402
from arbora.mcp_server import served_engine, served_invocation  # noqa: E402
from arbora.security.actor_context import (  # noqa: E402
    ActorContext,
    ActorType,
    CredentialLease,
)


def _session(**changes: Any) -> GraphSession:
    actor = ActorContext(
        actor_id="service:schema-test",
        actor_type=ActorType.AUTOMATED_SERVICE,
        tenant_id="tenant-a",
        authenticated=True,
        credential_lease=CredentialLease(int(time.time()) + 3600),
    )
    values: dict[str, Any] = {
        "actor": actor,
        "tenant": "tenant-a",
        "scopes": frozenset({"agent:component-read", "admin:decision-catalog"}),
        "graph": "tenant-a",
        "audience": "arbora",
        "policy_version": "policy-revision-test",
    }
    return GraphSession(**(values | changes))


class _Audit:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any], GraphSession]] = []
        self.attempts: list[str] = []
        self.existing: set[str] = set()
        self.fail = False

    def query_cypher(self, _query: str, _params: Any = None) -> list[Any]:
        resolve_session()
        return []

    def create_node(self, node_id: str, label: str, properties: Any) -> bool:
        bound = resolve_session()
        self.attempts.append(node_id)
        if self.fail:
            raise RuntimeError("fixture audit write refused")
        if node_id in self.existing:
            return False
        self.existing.add(node_id)
        self.records.append((node_id, label, dict(properties), bound))
        return True


class _Pool:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.sessions: list[GraphSession] = []

    async def connected_client(self, context: ClientContext, graph: str) -> Any:
        bound = resolve_session(graph=graph)
        assert bound.tenant == context.tenant
        self.sessions.append(bound)
        return self

    @asynccontextmanager
    async def bind(self, context: ClientContext, graph: str):
        bound = resolve_session(graph=graph)
        assert bound.tenant == context.tenant
        self.sessions.append(bound)
        yield self

    async def _send(self, method: str, params: Any, *_: Any, **__: Any) -> Any:
        assert method == "AgentComponent"
        if params["op"]["op"] == "current":
            return None
        self.sent.append(params)
        return {"component": {"definition_digest": "sha256:" + "d" * 64}}


@pytest.fixture
def fixture(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    policy_file = tmp_path / "schema-policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "defaults": {"tier": "approval_required"},
                "rules": [
                    {
                        "kind": task_plan_schema.PURPOSE_ID,
                        "target": task_plan_schema.COMPONENT_ID,
                        # The deployment may opt in this one internal,
                        # digest-bound publication action.  The embedded
                        # default remains approval_required below.
                        "tier": "auto_notify",
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("ACTION_POLICY_PATH", str(policy_file))
    action_policy.reset_action_policy_cache_for_tests()
    audit, pool = _Audit(), _Pool()
    sessions = [_session()]
    reads: list[GraphSession] = []

    def process_session() -> GraphSession:
        reads.append(sessions[0])
        return sessions[0]

    monkeypatch.setattr(served_engine, "ServedEngine", lambda _pool: audit)

    def connect_context(tenant: str) -> ClientContext:
        return ClientContext(
            principal=str(sessions[0].actor.actor_id),
            tenant=tenant,
            audience=str(sessions[0].audience),
            agent_id=str(sessions[0].actor.actor_id),
            roles=tuple(str(role) for role in sessions[0].actor.roles),
            scopes=tuple(str(scope) for scope in sessions[0].scopes),
            policy_version=str(sessions[0].policy_version),
        )

    bindings, startup = served_invocation._with_task_plan_schema(
        {}, pool, "tenant-a", connect_context, process_session
    )
    assert bindings is not None and startup is not None
    pins = bindings["task_plan_schema_pins"]
    yield SimpleNamespace(
        publish=served_invocation._task_plan_schema_publisher(
            pool, "tenant-a", connect_context, process_session
        ),
        startup=startup,
        bindings=bindings,
        pins=pins,
        pin=pins.pin,
        reads=reads,
        audit=audit,
        pool=pool,
        sessions=sessions,
    )
    action_policy.reset_action_policy_cache_for_tests()


def _publish(fixture: Any) -> str:
    return asyncio.run(fixture.publish("tenant-a", fixture.pin))


def test_schema_publication_uses_new_exact_durable_policy_receipt(fixture: Any) -> None:
    assert current_session() is None
    _publish(fixture)
    _publish(fixture)
    assert current_session() is None
    assert len(fixture.pool.sent) == len(fixture.audit.records) == 2
    assert fixture.audit.records[0][0] != fixture.audit.records[1][0]
    for sent, (identity, label, record, bound) in zip(
        fixture.pool.sent, fixture.audit.records, strict=True
    ):
        request = sent["op"]["request"]
        context = request["context"]
        intent = served_invocation._schema_action(bound, "tenant-a", fixture.pin)
        assert (
            label == "ActionDecision"
            and record["decision"] == action_policy.DECISION_ALLOW_NOTIFY
        )
        assert bound is fixture.sessions[0]
        assert record["request_digest"] == intent.digest()
        assert context["policy_digest"] == f"sha256:{intent.digest()}"
        assert request["component"]["policy_digest"] == context["policy_digest"]
        assert context["policy_decision_id"] == identity
        assert context["policy_revision"] == "policy-revision-test"


@pytest.mark.spec("ARBORA-RELEASE-R003.4.4")
def test_schema_publication_uses_native_opaque_principal_shape(
    fixture: Any,
) -> None:
    """The pre-admission context uses EG's canonical principal encoding."""
    _publish(fixture)
    context = fixture.pool.sent[0]["op"]["request"]["context"]
    expected = "principal:sha256:" + hashlib.sha256(b"service:schema-test").hexdigest()
    assert context["principal"] == expected
    assert context["caller_principal"] == expected
    assert context["actor_scope"] == expected
    assert context["principal"].startswith("principal:sha256:")
    assert len(context["principal"].removeprefix("principal:sha256:")) == 64


def test_schema_policy_override_is_exact_kind_and_target(fixture: Any) -> None:
    """The deployment opt-in cannot widen to neighboring publication targets."""
    policy = action_policy.get_action_policy(fixture.audit)
    intent = served_invocation._schema_action(
        fixture.sessions[0], "tenant-a", fixture.pin
    )

    assert policy.classify(intent) == action_policy.TIER_AUTO_NOTIFY
    assert policy.classify(replace(intent, target="arbora.agents.plan_run.other")) == (
        action_policy.TIER_APPROVAL
    )


def test_schema_publication_accepts_eg_admin_aggregate_scope(fixture: Any) -> None:
    """The live service principal's ``kg:admin`` satisfies EG's admin action."""
    bound = fixture.sessions[0]
    fixture.sessions[0] = replace(
        bound,
        scopes=frozenset({"agent:component-read", "kg:admin"}),
    )
    assert _publish(fixture) == "sha256:" + "d" * 64


def test_schema_publication_rejects_coarse_writer_without_admin(fixture: Any) -> None:
    """A normal graph writer cannot publish EG's admin-only FeatureSchema."""
    bound = fixture.sessions[0]
    fixture.sessions[0] = replace(
        bound,
        scopes=frozenset({"agent:component-read", "kg:write"}),
    )
    with pytest.raises(PermissionError):
        _publish(fixture)
    assert fixture.pool.sent == fixture.audit.records == []


def test_schema_publication_rejects_expired_process_authority(fixture: Any) -> None:
    process = fixture.sessions[0]
    expired = int(time.time()) - 1
    expired_process = replace(
        process,
        actor=replace(
            process.actor,
            credential_expires_at=expired,
            credential_lease=CredentialLease(expired),
        ),
    )
    fixture.sessions[0] = expired_process
    request = replace(process, trace_context="request-valid")

    with use_session(request):
        with pytest.raises(SessionExpiredError):
            _publish(fixture)
    assert fixture.pool.sent == []
    assert fixture.audit.records == []


@pytest.mark.parametrize("defect", ["digest", "action", "missing", "empty_id"])
def test_schema_refuses_wrong_receipt_before_publication(
    fixture: Any, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    policy = action_policy.get_action_policy(fixture.audit)
    decide = policy.decide

    def corrupted(request: Any) -> Any:
        decision = decide(request)
        assert decision.receipt is not None
        if defect == "digest":
            decision.receipt = replace(decision.receipt, request_digest="b" * 64)
        elif defect == "action":
            decision.request = replace(request, kind="connector_pack_import")
        elif defect == "missing":
            decision.receipt = None
        else:
            # A malformed owner reply is never an acceptable receipt instance.
            monkeypatch.setattr(
                decision,
                "receipt",
                SimpleNamespace(
                    authorizes_effect=True,
                    request_digest=request.digest(),
                    receipt_id="",
                ),
            )
        return decision

    monkeypatch.setattr(policy, "decide", corrupted)
    with pytest.raises(PermissionError, match="exact effect-authorizing receipt"):
        _publish(fixture)
    assert fixture.pool.sent == []


@pytest.mark.parametrize("fresh", ["allow", "deny", "hold"])
def test_schema_refuses_an_older_same_intent_receipt(
    fixture: Any, monkeypatch: pytest.MonkeyPatch, fresh: str
) -> None:
    policy = action_policy.get_action_policy(fixture.audit)
    request = served_invocation._schema_action(
        fixture.sessions[0], "tenant-a", fixture.pin
    )
    with use_session(fixture.sessions[0]):
        prior = policy.decide(request)
    assert prior.allowed and prior.receipt is not None
    decide = policy.decide

    def substituted(intent: Any) -> Any:
        decision = decide(intent)
        assert decision.audit_id != prior.audit_id
        decision.receipt = prior.receipt
        if fresh == "deny":
            decision.decision = action_policy.DECISION_DENY
            decision.audit_id = prior.audit_id
        elif fresh == "hold":
            decision.decision = action_policy.DECISION_QUEUE
            decision.approval_id = "action_approval:held"
            decision.audit_id = prior.audit_id
        return decision

    monkeypatch.setattr(policy, "decide", substituted)
    with pytest.raises(PermissionError, match="exact effect-authorizing receipt"):
        _publish(fixture)
    assert fixture.pool.sent == []


@pytest.mark.parametrize("defect", ["actor", "revision", "pin", "retired"])
def test_schema_revalidates_exact_intent_after_policy(
    fixture: Any, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    policy = action_policy.get_action_policy(fixture.audit)
    decide = policy.decide

    def changed(request: Any) -> Any:
        decision = decide(request)
        bound = fixture.sessions[0]
        if defect == "actor":
            fixture.sessions[0] = replace(
                bound, actor=replace(bound.actor, actor_id="service:other")
            )
        elif defect == "revision":
            fixture.sessions[0] = replace(bound, policy_version="policy-replaced")
        elif defect == "pin":
            fixture.pin["content_digest"] = "sha256:" + "e" * 64
        else:
            bound.actor.credential_lease.revoke("fixture retired")
        return decision

    monkeypatch.setattr(policy, "decide", changed)
    with pytest.raises(PermissionError):
        _publish(fixture)
    assert fixture.pool.sent == []


@pytest.mark.parametrize("defect", ["tenant", "graph", "scope"])
def test_schema_refuses_wrong_bound_authority(fixture: Any, defect: str) -> None:
    bound = fixture.sessions[0]
    if defect == "graph":
        fixture.sessions[0] = replace(bound, graph="other-graph")
    elif defect == "scope":
        fixture.sessions[0] = replace(bound, scopes=frozenset({"agent:component-read"}))
    tenant = "tenant-other" if defect == "tenant" else "tenant-a"
    with pytest.raises(PermissionError):
        asyncio.run(fixture.publish(tenant, fixture.pin))
    assert fixture.pool.sent == []
    assert fixture.audit.records == []


@pytest.mark.parametrize("failure", ["exception", "collision"])
def test_schema_failed_audit_has_no_authorizing_receipt_or_publication(
    fixture: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "exception":
        fixture.audit.fail = True
    else:
        fixture.audit.existing.add("action_decision:collision")
        monkeypatch.setattr(
            action_policy.uuid, "uuid4", lambda: SimpleNamespace(hex="collision")
        )
    policy = action_policy.get_action_policy(fixture.audit)
    decide = policy.decide
    decisions = []

    def recorded(request: Any) -> Any:
        decision = decide(request)
        decisions.append(decision)
        return decision

    monkeypatch.setattr(policy, "decide", recorded)
    with pytest.raises(PermissionError, match="exact effect-authorizing receipt"):
        _publish(fixture)
    assert fixture.pool.sent == fixture.audit.records == []
    assert len(decisions) == 1
    assert decisions[0].disposition is action_policy.PolicyDisposition.UNAVAILABLE
    assert decisions[0].receipt is None
    if failure == "collision":
        assert set(fixture.audit.attempts) == {"action_decision:collision"}
        assert fixture.audit.existing == {"action_decision:collision"}


def test_default_schema_action_stays_held(
    fixture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ACTION_POLICY_PATH")
    action_policy.reset_action_policy_cache_for_tests()
    with pytest.raises(PermissionError, match="exact effect-authorizing receipt"):
        _publish(fixture)
    assert fixture.pool.sent == []
    assert [row[1] for row in fixture.audit.records] == [
        "ActionApproval",
        "ActionDecision",
    ]
    assert fixture.audit.records[-1][2]["decision"] == "queue_approval"


def test_matching_component_reuse_needs_no_new_write_receipt(
    fixture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from medulla.generated import storage

    async def current(*_: Any, **__: Any) -> Any:
        return SimpleNamespace(
            content_digest=fixture.pin["content_digest"],
            definition_digest="sha256:" + "c" * 64,
        )

    monkeypatch.setattr(storage, "send_agent_component_current", current)
    fixture.sessions[0] = replace(
        fixture.sessions[0], scopes=frozenset({"agent:component-read"})
    )
    fixture.audit.fail = True
    assert _publish(fixture) == "sha256:" + "c" * 64
    assert fixture.pool.sent == fixture.audit.records == []


def _request(fixture: Any) -> GraphSession:
    """A caller whose verified session differs from the process authority."""
    process = fixture.sessions[0]
    return replace(
        process,
        actor=replace(process.actor, actor_id="user:caller"),
        scopes=frozenset({"kg:write", "query:decide", "work:submit"}),
        trace_context="request-trace",
    )


def _plan_context(fixture: Any, tenant: str = "tenant-a") -> Any:
    return SimpleNamespace(
        services=dict(fixture.bindings), caller=SimpleNamespace(tenant=tenant)
    )


@pytest.mark.spec("ARBORA-RELEASE-R003.4.4")
def test_startup_publishes_the_served_tenant_under_the_process_authority(
    fixture: Any,
) -> None:
    assert current_session() is None
    assert asyncio.run(fixture.startup()) == {"tenant-a": True}
    assert current_session() is None
    assert fixture.pool.sessions == [fixture.sessions[0]]
    assert len(fixture.pool.sent) == 1
    assert fixture.pins.pin_for("tenant-a")["definition_digest"] == (
        "sha256:" + "d" * 64
    )


@pytest.mark.spec("ARBORA-RELEASE-R003.4.4")
def test_caller_with_other_scopes_plans_once_the_pin_is_published(
    fixture: Any,
) -> None:
    from arbora.api.ops import plans

    asyncio.run(fixture.startup())
    sent, reads = len(fixture.pool.sent), len(fixture.reads)
    request = _request(fixture)
    with use_session(request):
        pin = asyncio.run(plans._feature_schema(_plan_context(fixture), "tenant-a"))
        assert current_session() is request
    assert pin == fixture.pins.pin_for("tenant-a")
    assert (len(fixture.pool.sent), len(fixture.reads)) == (sent, reads)


@pytest.mark.spec("ARBORA-RELEASE-R003.4.4")
def test_unpublished_request_refuses_unavailable_and_does_not_publish(
    fixture: Any,
) -> None:
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.api.ops import plans

    request = _request(fixture)
    with use_session(request), pytest.raises(OperationRefused) as refused:
        asyncio.run(plans._feature_schema(_plan_context(fixture), "tenant-a"))
    assert refused.value.code == "UNAVAILABLE"
    assert fixture.pool.sent == fixture.pool.sessions == fixture.audit.records == []
    assert fixture.reads == [] and fixture.pins.pin_for("tenant-a") is None


@pytest.mark.spec("ARBORA-RELEASE-R003.4.4")
def test_no_request_path_can_install_the_process_session(fixture: Any) -> None:
    """Request bindings hold only the read-only pins; only the lifespan publishes."""
    import ast
    from pathlib import Path

    import arbora

    assert set(fixture.bindings) == {"task_plan_schema_pins"}
    assert not any(callable(value) for value in fixture.bindings.values())
    package = Path(arbora.__file__).parent
    readers = sorted(
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if any(
            isinstance(node, ast.Attribute)
            and node.attr == "process_startup"
            and isinstance(node.ctx, ast.Load)
            for node in ast.walk(ast.parse(path.read_text()))
        )
    )
    assert readers == ["mcp_server/server.py"]
    publishers = sorted(
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if "_task_plan_schema_publisher(" in path.read_text()
        or ".publish_for(" in path.read_text()
    )
    assert publishers == ["api/task_plan_schema.py", "mcp_server/served_invocation.py"]
