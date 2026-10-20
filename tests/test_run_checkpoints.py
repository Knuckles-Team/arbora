"""Checkpoint host CAS and authority boundaries with injected native transport."""

import contextlib
import hashlib
from types import SimpleNamespace

import pytest

from arbora.admitted_run import AdmittedClaim
from arbora.run_checkpoints import RunCheckpointPort


@pytest.fixture
def port(monkeypatch):
    import arbora.run_checkpoints as module

    monkeypatch.setattr(module, "use_session", lambda _: contextlib.nullcontext())
    binding = AdmittedClaim(
        SimpleNamespace(),
        SimpleNamespace(tenant="tenant", engine_verified_context=lambda: None),
        "run",
        "item",
        {
            "lease_holder_ref": "worker",
            "lease_epoch": 2,
            "fencing_token": 3,
            "lease_expires_at_ms": 2**63,
        },
    )
    return RunCheckpointPort(binding, SimpleNamespace())


def receipt(payload):
    return {
        "revision": (payload["expected_revision"] or 0) + 1,
        "replayed": False,
        "payload_digest": payload["snapshot"]["payload_digest"],
        "work_item_id": payload["work_item_id"],
        "run_id": payload["run_id"],
    }


@pytest.mark.asyncio
async def test_restore_then_publish_carries_native_fence_and_cas(port):
    calls = []

    async def restore(**payload):
        calls.append(payload)
        return None

    async def publish(**payload):
        calls.append(payload)
        return receipt(payload)

    port.client.get_checkpoint = restore
    port.client.publish_checkpoint = publish
    assert await port.restore() is None
    await port.save('{"phase":"step_inflight"}')
    await port.save('{"phase":"completed"}')
    assert calls[0]["current_claim"] == {
        "worker_id": "worker",
        "lease_epoch": 2,
        "fencing_token": 3,
    }
    assert calls[1]["expected_revision"] is None
    assert calls[2]["expected_revision"] == 1
    assert port.revision == 2
    port.binding.active = False
    with pytest.raises(RuntimeError, match="closed"):
        await port.save("{}")
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_uncertain_write_only_allows_exact_request_retry(port):
    attempts = []

    async def publish(**payload):
        attempts.append(payload)
        if len(attempts) == 1:
            raise OSError("transport unavailable")
        return receipt(payload)

    port.client.publish_checkpoint = publish
    port.restored = True
    with pytest.raises(RuntimeError, match="native checkpoint is unavailable"):
        await port.save("first")
    with pytest.raises(RuntimeError, match="uncertain"):
        await port.save("changed")
    await port.save("first")
    assert attempts[0] == attempts[1]
    assert port.revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"revision": True},
        {"revision": 0},
        {"payload_digest": "foreign"},
        {"content": 7},
    ],
)
async def test_bad_restore_never_sets_revision(port, change):
    async def restore(**_):
        return {
            "revision": 4,
            "content": "{}",
            "payload_digest": hashlib.sha256(b"{}").hexdigest(),
            **change,
        }

    port.client.get_checkpoint = restore
    with pytest.raises(RuntimeError, match="projection"):
        await port.restore()
    assert not port.restored
    assert port.revision is None


@pytest.mark.asyncio
async def test_foreign_receipt_keeps_uncertain_write(port):
    async def publish(**payload):
        return {**receipt(payload), "work_item_id": "foreign"}

    port.client.publish_checkpoint = publish
    port.restored = True
    with pytest.raises(RuntimeError, match="receipt"):
        await port.save("{}")
    assert port.pending is not None
    assert port.revision is None


@pytest.mark.asyncio
async def test_independent_port_attempts_do_not_share_idempotency_key(port):
    other = RunCheckpointPort(port.binding, port.client)
    seen = []

    async def publish(**payload):
        seen.append(payload)
        return receipt(payload)

    port.client.publish_checkpoint = publish
    port.restored = other.restored = True
    await port.save("{}")
    await other.save("{}")
    assert seen[0]["idempotency_key"] != seen[1]["idempotency_key"]
    assert seen[0]["expected_revision"] == seen[1]["expected_revision"] is None


@pytest.mark.asyncio
async def test_checkpoint_scope_consumes_admission_once(port, monkeypatch):
    import arbora.run_checkpoints as module

    port.client.get_checkpoint = lambda: None
    port.client.publish_checkpoint = lambda: None
    port.binding.engine = SimpleNamespace(
        graph_compute=SimpleNamespace(
            for_graph=lambda _: SimpleNamespace(
                async_client=SimpleNamespace(work_items=port.client)
            )
        )
    )
    monkeypatch.setattr(module, "current_admitted_claim", lambda _: port.binding)
    plan = SimpleNamespace(toolsets=(), harness=SimpleNamespace(plan_scaffold="off"))
    async with module.checkpoint_scope(object(), plan) as active:
        assert active is not None
        with pytest.raises(RuntimeError, match="already consumed"):
            async with module.checkpoint_scope(object(), plan):
                pytest.fail("second execution admitted")


@pytest.mark.asyncio
@pytest.mark.parametrize("toolsets,scaffold", [((), "on"), ((object(),), "off")])
async def test_unsupported_plan_does_not_gain_checkpoint_capability(
    port, monkeypatch, toolsets, scaffold
):
    import arbora.run_checkpoints as module

    monkeypatch.setattr(module, "current_admitted_claim", lambda _: port.binding)
    plan = SimpleNamespace(
        toolsets=toolsets, harness=SimpleNamespace(plan_scaffold=scaffold)
    )
    async with module.checkpoint_scope(object(), plan) as active:
        assert active is None
    assert not port.binding.adapters
