"""Real AU callback composition with explicit native transport doubles.

These are source-contract tests, not installed-engine acceptance.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
from agent_utilities import host_ports
from agent_utilities.harness.plan import PlanItem, PlanState

from arbora import current_plan_publication as publication
from arbora.deployment.semantic_provisioning import CONTROL_GRAPH

REAL_ADMIT = publication._admit

pytestmark = pytest.mark.spec("ARBORA-OPS-R021.4.5.1.2")


from tests.a2a.test_operation_bridge import session as Session


def claim(item: str = "work-item:one") -> dict[str, Any]:
    return {
        "tenant": "tenant:one",
        "claimed": True,
        "work_item_id": item,
        "lease_holder_ref": "worker",
        "lease_epoch": 3,
        "fencing_token": 7,
        "lease_expires_at_ms": 2000,
    }


@asynccontextmanager
async def admitted_publication_scope(
    request: Any,
) -> AsyncIterator[host_ports.ScaffoldPublisher]:
    """Published AU's required factory is used only with an admitted fixture."""
    async with publication.publication_scope(request) as publisher:
        assert publisher is not None
        yield publisher


def register_publication_fixture() -> None:
    host_ports.register_optional_scaffold_publication_factory(
        publication.publication_scope
    )


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Any:
    from epistemic_graph.generated import coordination

    calls: list[Any] = []

    async def send(
        client: Any, params: Any, graph: str, *, idempotency_key: str
    ) -> Any:
        assert idempotency_key == params["request"]["idempotency_key"]
        assert graph == CONTROL_GRAPH
        calls.append(params["request"])
        return params

    monkeypatch.setattr(coordination, "send_publish_current_plan", send, raising=False)
    monkeypatch.setattr(
        coordination, "decode_publish_current_plan", lambda x: x, raising=False
    )
    monkeypatch.setattr(publication.time, "time_ns", lambda: 1000_000_000)

    async def read(*args):
        return None

    monkeypatch.setattr(coordination, "send_get_current_plan", read)
    monkeypatch.setattr(coordination, "decode_get_current_plan", lambda x: x)

    async def admit(*args):
        pass

    monkeypatch.setattr(publication, "_admit", admit)
    register_publication_fixture()
    yield calls
    host_ports.register_optional_scaffold_publication_factory(None)


def engine() -> Any:
    return SimpleNamespace(
        graph_compute=SimpleNamespace(
            for_graph=lambda graph: SimpleNamespace(
                async_client=SimpleNamespace(
                    use_verified_context=lambda context: nullcontext()
                )
            )
        )
    )


@pytest.mark.asyncio
async def test_actual_au_revisions_use_exact_claim_and_close(transport: Any) -> None:
    session = Session()
    request = SimpleNamespace(run_id="run", metadata={"session": session})
    state = PlanState()
    with publication.bind_admitted_claim(
        engine(),
        hosted_invocation=object(),
        session=session,
        run_id="run",
        work_item_id="work-item:one",
        claim=claim(),
    ):
        async with host_ports.scaffold_publication_scope(request, state) as publisher:
            assert publisher is not None
            await publisher.publish(
                *state.replace_versioned([PlanItem(text="inspect", status="done")])
            )
    assert [p["snapshot"]["revision"] for p in transport] == [0, 1]
    assert transport[0]["snapshot"]["items"] == []
    assert transport[1]["snapshot"]["items"] == [{"text": "inspect", "status": "done"}]
    assert all(
        (
            p["worker_id"],
            p["lease_epoch"],
            p["fencing_token"],
            p["snapshot"]["expires_at_ms"],
        )
        == ("worker", 3, 7, 2000)
        for p in transport
    )
    with pytest.raises(RuntimeError, match="closed"):
        await publisher.publish(2, ())


@pytest.mark.asyncio
async def test_retry_preserves_exact_payload_and_binding_expiry(
    transport: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = Session()
    request = SimpleNamespace(run_id="run", metadata={"session": session})
    with publication.bind_admitted_claim(
        engine(),
        hosted_invocation=object(),
        session=session,
        run_id="run",
        work_item_id="work-item:one",
        claim=claim(),
    ):
        async with publication.publication_scope(request) as publisher:
            assert publisher is not None
            await publisher.publish(0, ())
            monkeypatch.setattr(publication.time, "time_ns", lambda: 1500_000_000)
            await publisher.publish(0, ())
            assert transport[0] == transport[1]
            with pytest.raises(ValueError, match="conflict"):
                await publisher.publish(
                    0, (PlanItem(text="changed", status="pending"),)
                )
            monkeypatch.setattr(publication.time, "time_ns", lambda: 2000_000_000)
            with pytest.raises(RuntimeError, match="expired"):
                await publisher.publish(1, ())
    with pytest.raises(RuntimeError, match="closed"):
        await publisher.publish(2, ())


@pytest.mark.parametrize(
    "change",
    [
        {"tenant": "foreign"},
        {"claimed": False},
        {"work_item_id": "other"},
        {"lease_epoch": True},
        {"fencing_token": 0},
        {"lease_expires_at_ms": None},
        {"lease_holder_ref": ""},
    ],
)
def test_invalid_claim_never_binds(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        with publication.bind_admitted_claim(
            engine(),
            hosted_invocation=object(),
            session=Session(),
            run_id="run",
            work_item_id="work-item:one",
            claim={**claim(), **change},
        ):
            pytest.fail("invalid claim bound")


@pytest.mark.asyncio
async def test_concurrent_bindings_keep_run_and_session_isolated(
    transport: Any,
) -> None:
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def run(name: str) -> None:
        session = Session()
        request = SimpleNamespace(run_id=name, metadata={"session": session})
        with publication.bind_admitted_claim(
            engine(),
            hosted_invocation=object(),
            session=session,
            run_id=name,
            work_item_id="work-item:one",
            claim=claim(),
        ):
            async with publication.publication_scope(request) as publisher:
                assert publisher is not None
                if name == "one":
                    entered.set()
                    await finish.wait()
                else:
                    await entered.wait()
                    finish.set()
                await publisher.publish(0, ())
                with pytest.raises(RuntimeError, match="session differs"):
                    async with publication.publication_scope(
                        SimpleNamespace(run_id=name, metadata={"session": Session()})
                    ):
                        pytest.fail("foreign session bound")

    await asyncio.gather(run("one"), run("two"))
    assert {p["snapshot"]["run_id"] for p in transport} == {"one", "two"}


async def test_run_without_native_binding_keeps_local_scaffold(transport):
    request = SimpleNamespace(run_id="legacy", metadata={})
    state = PlanState()
    async with publication.publication_scope(request) as publisher:
        assert publisher is None
        state.replace_versioned((PlanItem(text="local", status="pending"),))
    assert state.read_versioned()[0] == 1
    assert transport == []


async def test_copied_task_context_cannot_publish_after_admission_scope_closes(
    transport,
):
    released = asyncio.Event()
    session = Session()
    request = SimpleNamespace(run_id="run", metadata={"session": session})

    async def delayed():
        await released.wait()
        with pytest.raises(RuntimeError, match="closed"):
            async with publication.publication_scope(request):
                pytest.fail("closed copied binding became active")

    with publication.bind_admitted_claim(
        engine(),
        hosted_invocation=object(),
        session=session,
        run_id="run",
        work_item_id="work-item:one",
        claim=claim(),
    ):
        child = asyncio.create_task(delayed())
    released.set()
    await child
    assert transport == []


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [False, True])
async def test_existing_task_plan_revision_is_preserved_or_conflicts(
    transport, monkeypatch, concurrent
):
    """Controlled native rules qualify routing, not native atomicity itself."""
    from epistemic_graph.generated import coordination

    stored = {"run_id": "run", "revision": 0, "task_plan": {"plan_digest": "original"}}
    attempted = []

    async def read(*args):
        return SimpleNamespace(model_dump=lambda **kw: dict(stored))

    async def publish(client, params, graph, **kwargs):
        payload = params["request"]
        attempted.append(payload)
        snapshot = payload["snapshot"]
        if snapshot["revision"] <= stored["revision"]:
            raise RuntimeError("current-plan revision must increase")
        stored.update(snapshot)  # Native EG retains an omitted task_plan.
        return payload

    monkeypatch.setattr(coordination, "send_get_current_plan", read)
    monkeypatch.setattr(coordination, "send_publish_current_plan", publish)
    session = Session()
    request = SimpleNamespace(run_id="run", metadata={"session": session})
    with publication.bind_admitted_claim(
        engine(),
        hosted_invocation=object(),
        session=session,
        run_id="run",
        work_item_id="work-item:one",
        claim=claim(),
    ):
        async with publication.publication_scope(request) as publisher:
            if concurrent:
                stored["revision"] = 1
                for _ in range(2):
                    with pytest.raises(RuntimeError, match="revision must increase"):
                        await publisher.publish(0, ())
                assert attempted[0] == attempted[1]
                assert publisher.revision_offset == 1
            else:
                await publisher.publish(0, ())
                assert stored["revision"] == 1
                await publisher.publish(1, (PlanItem(text="inspect", status="done"),))
                assert stored["revision"] == 2
            assert stored["task_plan"] == {"plan_digest": "original"}
            assert all("task_plan" not in p["snapshot"] for p in attempted)


@pytest.mark.asyncio
async def test_uncertain_write_retries_exact_mapped_payload(transport, monkeypatch):
    from epistemic_graph.generated import coordination

    attempts = []

    async def uncertain(client, params, graph, **kwargs):
        attempts.append(params)
        if len(attempts) == 1:
            raise TimeoutError("unknown native outcome")
        return params

    monkeypatch.setattr(coordination, "send_publish_current_plan", uncertain)
    session = Session()
    with publication.bind_admitted_claim(
        engine(),
        hosted_invocation=object(),
        session=session,
        run_id="run",
        work_item_id="work-item:one",
        claim=claim(),
    ):
        async with publication.publication_scope(
            SimpleNamespace(run_id="run", metadata={"session": session})
        ) as publisher:
            with pytest.raises(TimeoutError):
                await publisher.publish(0, ())
            monkeypatch.setattr(publication.time, "time_ns", lambda: 1500_000_000)
            await publisher.publish(0, ())
            assert attempts[0] == attempts[1]
            assert attempts[1]["request"]["snapshot"]["expires_at_ms"] == 2000


@pytest.mark.asyncio
async def test_explicit_anonymous_transport_does_not_gain_verified_authority(
    transport, monkeypatch
):
    from arbora.security import anonymous

    monkeypatch.setattr(anonymous, "_ACTIVE_MODE", "none")
    session = anonymous.session()
    client = SimpleNamespace(authenticated=False)
    anonymous_engine = SimpleNamespace(
        graph_compute=SimpleNamespace(
            for_graph=lambda graph: SimpleNamespace(async_client=client)
        )
    )
    with publication.bind_admitted_claim(
        anonymous_engine,
        hosted_invocation=object(),
        session=session,
        run_id="run",
        work_item_id="work-item:one",
        claim={**claim(), "tenant": "local"},
    ):
        async with host_ports.scaffold_publication_scope(
            SimpleNamespace(run_id="run", metadata={"session": session}), PlanState()
        ):
            pass
    assert transport[0]["tenant"] == "local"


@pytest.mark.asyncio
async def test_verified_session_refuses_anonymous_client_before_read(transport):
    session = Session()
    wrong_engine = SimpleNamespace(
        graph_compute=SimpleNamespace(
            for_graph=lambda graph: SimpleNamespace(
                async_client=SimpleNamespace(authenticated=False)
            )
        )
    )
    with publication.bind_admitted_claim(
        wrong_engine,
        hosted_invocation=object(),
        session=session,
        run_id="run",
        work_item_id="work-item:one",
        claim=claim(),
    ):
        with pytest.raises(PermissionError, match="anonymous transport"):
            async with publication.publication_scope(
                SimpleNamespace(run_id="run", metadata={"session": session})
            ):
                pytest.fail("wrong transport accepted")
    assert transport == []


@pytest.mark.asyncio
@pytest.mark.parametrize("deny_at", [1, 2, 3, 4])
async def test_host_operation_policy_is_rechecked_around_native_awaits(
    transport, monkeypatch, deny_at
):
    from arbora.api.registry import Effect, Registry
    from tests.api.test_invoke_contract import operation, setup_services

    checked = []

    class Policy:
        async def check_op(self, operation, caller):
            checked.append((operation.id, caller.principal))
            return len(checked) != deny_at

    registry = Registry(
        tuple(
            operation(id=name, effect=Effect.READ)
            for name in (
                "eg.coordination.GetCurrentPlan",
                "eg.coordination.PublishCurrentPlan",
            )
        )
    )
    services, *_ = setup_services(
        registry=registry, policy_gate=Policy(), external_effect_operations=frozenset()
    )
    monkeypatch.setattr(publication, "_admit", REAL_ADMIT)
    session = Session()
    with publication.bind_admitted_claim(
        engine(),
        hosted_invocation=SimpleNamespace(services=services),
        session=session,
        run_id="run",
        work_item_id="work-item:one",
        claim=claim(),
    ):
        with pytest.raises(RuntimeError, match="scaffold publication"):
            async with host_ports.scaffold_publication_scope(
                SimpleNamespace(run_id="run", metadata={"session": session}),
                PlanState(),
            ):
                pytest.fail("denied publication accepted")
    assert [name for name, _ in checked] == [
        "eg.coordination.GetCurrentPlan",
        "eg.coordination.GetCurrentPlan",
        "eg.coordination.PublishCurrentPlan",
        "eg.coordination.PublishCurrentPlan",
    ][:deny_at]
    assert len(checked) == deny_at
    assert len(transport) == (1 if deny_at == 4 else 0)


@pytest.mark.asyncio
async def test_concurrent_scope_initialization_reads_baseline_once(
    transport, monkeypatch
):
    from epistemic_graph.generated import coordination

    reads = []

    async def read(*args):
        reads.append(args)
        await asyncio.sleep(0)
        return SimpleNamespace(model_dump=lambda **kw: {"run_id": "run", "revision": 4})

    monkeypatch.setattr(coordination, "send_get_current_plan", read)
    session = Session()
    request = SimpleNamespace(run_id="run", metadata={"session": session})

    async def enter():
        async with publication.publication_scope(request) as publisher:
            assert publisher.revision_offset == 5

    with publication.bind_admitted_claim(
        engine(),
        hosted_invocation=object(),
        session=session,
        run_id="run",
        work_item_id="work-item:one",
        claim=claim(),
    ):
        await asyncio.gather(enter(), enter())
    assert len(reads) == 1


@pytest.mark.asyncio
async def test_lease_expiry_during_policy_await_stops_dispatch(transport, monkeypatch):
    async def admit(binding, operation):
        if operation.endswith("PublishCurrentPlan"):
            monkeypatch.setattr(publication.time, "time_ns", lambda: 2000_000_000)

    monkeypatch.setattr(publication, "_admit", admit)
    session = Session()
    with publication.bind_admitted_claim(
        engine(),
        hosted_invocation=object(),
        session=session,
        run_id="run",
        work_item_id="work-item:one",
        claim=claim(),
    ):
        with pytest.raises(RuntimeError, match="scaffold publication failed"):
            async with host_ports.scaffold_publication_scope(
                SimpleNamespace(run_id="run", metadata={"session": session}),
                PlanState(),
            ):
                pytest.fail("expired publication accepted")
    assert transport == []
