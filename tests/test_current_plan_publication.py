"""Real AU callback composition with explicit native transport doubles.

These are source-contract tests, not installed-engine acceptance.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from agent_utilities import host_ports
from agent_utilities.harness.plan import PlanItem, PlanState

from arbora import current_plan_publication as publication
from arbora.deployment.semantic_provisioning import CONTROL_GRAPH

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


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Any:
    from epistemic_graph.generated import coordination

    calls: list[Any] = []

    async def send(client: Any, params: Any, graph: str) -> Any:
        assert graph == CONTROL_GRAPH
        calls.append(params["request"])
        return params

    monkeypatch.setattr(coordination, "send_publish_current_plan", send, raising=False)
    monkeypatch.setattr(
        coordination, "decode_publish_current_plan", lambda x: x, raising=False
    )
    monkeypatch.setattr(publication.time, "time_ns", lambda: 1000_000_000)
    getattr(
        host_ports,
        "register_optional_scaffold_publication_factory",
        host_ports.register_scaffold_publication_factory,
    )(publication.publication_scope)
    yield calls
    host_ports.register_scaffold_publication_factory(None)


def engine() -> Any:
    return SimpleNamespace(
        graph_compute=SimpleNamespace(
            for_graph=lambda graph: SimpleNamespace(async_client=object())
        )
    )


@pytest.mark.asyncio
async def test_actual_au_revisions_use_exact_claim_and_close(transport: Any) -> None:
    session = Session()
    request = SimpleNamespace(run_id="run", metadata={"session": session})
    state = PlanState()
    with publication.bind_admitted_claim(
        engine(),
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
        session=session,
        run_id="run",
        work_item_id="work-item:one",
        claim=claim(),
    ):
        async with publication.publication_scope(request) as publisher:
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
            session=Session(),
            run_id="run",
            work_item_id="work-item:one",
            claim={**claim(), **change},
        ):
            pytest.fail("invalid claim bound")


@pytest.mark.asyncio
async def test_concurrent_bindings_and_copied_context_close(transport: Any) -> None:
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def run(name: str) -> None:
        session = Session()
        request = SimpleNamespace(run_id=name, metadata={"session": session})
        with publication.bind_admitted_claim(
            engine(),
            session=session,
            run_id=name,
            work_item_id="work-item:one",
            claim=claim(),
        ):
            async with publication.publication_scope(request) as publisher:
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
