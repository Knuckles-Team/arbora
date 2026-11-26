"""The engine host checkpoint port: save, restore, fence and isolation."""

from __future__ import annotations

import asyncio
import hashlib
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from arbora.a2a.checkpoint import (
    CheckpointUnavailable,
    EngineHostCheckpoint,
    checkpoint_store,
)
from arbora.agent_runs import NativeRunContext


def owner(tenant: str = "tenant:one", run_id: str = "run:one") -> NativeRunContext:
    return NativeRunContext(
        run_id,
        "task:one",
        "context:one",
        "work:one",
        SimpleNamespace(tenant=tenant),  # type: ignore[arg-type]
        MappingProxyType(
            {"lease_holder_ref": "worker:one", "lease_epoch": 3, "fencing_token": 7}
        ),
        "control",
    )


class Engine:
    """The engine's checkpoint rows: owner-bound, revision compare-and-set."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.down = False

    async def _send(
        self, method: str, params: dict[str, Any], graph: str | None, **_: Any
    ) -> Any:
        self.sent.append((method, params))
        if self.down:
            raise ConnectionError("engine unreachable")
        request = params["request"]
        key = (request["tenant"], request["run_id"])
        row = self.rows.get(key)
        if method == "GetRunCheckpoint":
            if row is None:
                return None
            return {
                "content": row["content"],
                "context_id": request["context_id"],
                "long_term": [],
                "short_term": [],
                "payload_digest": row["digest"],
                "revision": row["revision"],
                "work_item": None,
            }
        current = row["revision"] if row else None
        if request["expected_revision"] != current:
            raise RuntimeError("CONFLICT")
        snapshot = request["snapshot"]
        revision = 0 if current is None else current + 1
        self.rows[key] = {
            "content": snapshot["content"],
            "digest": snapshot["payload_digest"],
            "revision": revision,
        }
        return {
            "changed_work_item_ids": [request["work_item_id"]],
            "context_id": request["context_id"],
            "payload_digest": snapshot["payload_digest"],
            "replayed": False,
            "revision": revision,
            "run_id": request["run_id"],
            "work_item_id": request["work_item_id"],
        }


@pytest.fixture(autouse=True)
def lenient_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decode the doubles' answers without the engine's full work-item view."""
    import medulla.generated.coordination as coordination

    def get(result: Any) -> Any:
        payload = result.payload if hasattr(result, "payload") else result
        return None if payload is None else SimpleNamespace(**payload)

    monkeypatch.setattr(coordination, "decode_get_run_checkpoint", get)


def test_checkpoint_saves_restores_and_resumes_after_a_new_port() -> None:
    engine = Engine()
    first = EngineHostCheckpoint(engine, owner())

    async def run() -> None:
        assert await first.restore() is None
        await first.save("step-1")
        await first.save("step-2")
        # A restarted host builds a new port for the same run and resumes.
        second = EngineHostCheckpoint(engine, owner())
        assert await second.restore() == "step-2"
        await second.save("step-3")
        assert await EngineHostCheckpoint(engine, owner()).restore() == "step-3"
        # Another tenant or run reads as absent.
        assert await EngineHostCheckpoint(engine, owner("tenant:two")).restore() is None
        assert (
            await EngineHostCheckpoint(engine, owner(run_id="run:two")).restore()
            is None
        )

    asyncio.run(run())
    publishes = [p["request"] for m, p in engine.sent if m == "PublishRunCheckpoint"]
    assert [p["expected_revision"] for p in publishes] == [None, 0, 1]
    assert all(
        p["fencing_token"] == 7
        and p["lease_epoch"] == 3
        and p["worker_id"] == "worker:one"
        for p in publishes
    )


def test_a_stale_writer_conflicts_and_a_bad_digest_or_engine_refuses() -> None:
    engine = Engine()

    async def run() -> None:
        one = EngineHostCheckpoint(engine, owner())
        two = EngineHostCheckpoint(engine, owner())
        await one.restore()
        await two.restore()
        await one.save("a")
        with pytest.raises(CheckpointUnavailable):
            await two.save("b")  # its expected revision is stale
        engine.rows[("tenant:one", "run:one")]["digest"] = "0" * 64
        with pytest.raises(CheckpointUnavailable):
            await EngineHostCheckpoint(engine, owner()).restore()
        engine.down = True
        with pytest.raises(CheckpointUnavailable):
            await EngineHostCheckpoint(engine, owner()).restore()

    asyncio.run(run())
    assert hashlib.sha256(b"a").hexdigest() != "0" * 64


def test_store_binds_nothing_outside_a_native_run_scope() -> None:
    bind = checkpoint_store(lambda: None)

    async def run() -> Any:
        async with bind(SimpleNamespace(run_id="run:one", metadata={}), None) as port:
            return port

    assert asyncio.run(run()) is None
