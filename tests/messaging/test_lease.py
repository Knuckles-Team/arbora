"""The messaging intake lease runs on the engine's served WorkItem methods."""

from __future__ import annotations

import contextvars
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.messaging import lease as intake_lease


class FakeWorkItems:
    """The engine's WorkItem namespace: one owner per row, renewals by script."""

    def __init__(self, *, renew: Any = True) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.owner: dict[str, str] = {}
        self.deferred: list[str] = []
        self.renew_answer = renew
        self.lock = threading.Lock()

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        assert request["context"] == {"tenant_id": "tenant-a"}
        self.rows.setdefault(
            request["work_item_id"],
            {"kind": request["kind"], "metadata": request["metadata"]},
        )
        return {"created": True}

    def get(self, *, tenant: str, work_item_id: str) -> dict[str, Any] | None:
        return self.rows.get(work_item_id)

    def claim(self, request: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            item = request["work_item_id"]
            if item in self.owner:
                return {"claimed": False, "reason": "empty"}
            self.owner[item] = request["worker_ref"]
            return {"claimed": True, "lease_epoch": 1, "fencing_token": 1}

    def renew(self, *, work_item_id: str, **_: Any) -> dict[str, Any]:
        answer = self.renew_answer
        return {"renewed": answer(work_item_id) if callable(answer) else answer}

    def defer(self, *, work_item_id: str, **_: Any) -> dict[str, Any]:
        self.deferred.append(work_item_id)
        return {"deferred": True}


def _engine(work_items: Any) -> SimpleNamespace:
    return SimpleNamespace(
        work_items=work_items,
        _effective_verified_context=lambda: {"tenant_id": "tenant-a"},
    )


@pytest.fixture(autouse=True)
def _fixed_identity(monkeypatch):
    monkeypatch.setattr(intake_lease, "_identity_digest", lambda platform: "identity")
    monkeypatch.setattr(intake_lease, "_clock", lambda: 1_000.0)


SESSION = SimpleNamespace(tenant="tenant-a")


def _lease(platform: str) -> intake_lease.IntakeLease:
    return intake_lease.IntakeLease(
        platform=platform,
        item_id=f"workitem:messaging-intake:{platform}",
        claim={
            "tenant": "tenant-a",
            "worker_ref": "w",
            "lease_epoch": 1,
            "fencing_token": 1,
        },
        lease_ttl_s=0.03,
    )


@pytest.mark.spec("ARBORA-MESSAGING-R800.1001")
def test_two_contenders_cannot_both_own_intake():
    work_items = FakeWorkItems()
    engine = _engine(work_items)
    results: list[Any] = [None, None]
    barrier = threading.Barrier(2)

    def contend(index: int) -> None:
        barrier.wait()
        results[index] = intake_lease.acquire_intake_lease(engine, "telegram", SESSION)

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    owners = [result for result in results if result is not None]
    assert len(owners) == 1
    assert owners[0].claim["tenant"] == "tenant-a"
    assert len(work_items.rows) == 1


# spec: ARBORA-MESSAGING-R800.1002
@pytest.mark.parametrize(
    ("engine", "session"),
    [
        (SimpleNamespace(), SESSION),
        (_engine(FakeWorkItems()), SimpleNamespace(tenant="")),
    ],
    ids=["engine-serves-no-work-items", "session-without-tenant"],
)
def test_missing_authority_opens_no_poller(engine, session):
    assert intake_lease.acquire_intake_leases(engine, ["telegram"], session) == ()


def test_foreign_row_is_never_claimed():
    work_items = FakeWorkItems()
    item_id = intake_lease._work_item_id("telegram", "identity")
    work_items.rows[item_id] = {"kind": "other", "metadata": {}}
    with pytest.raises(intake_lease.WorkItemBackendUnavailable):
        intake_lease.acquire_intake_lease(_engine(work_items), "telegram", SESSION)
    assert work_items.owner == {}


def test_lost_renewal_stops_intake_and_releases_the_lease():
    work_items = FakeWorkItems(renew=False)
    stop = threading.Event()

    def serve(platforms, stop_event, platform_events):
        assert stop_event.wait(timeout=2.0)

    intake_lease.run_with_intake_leases(
        _engine(work_items), (_lease("telegram"),), stop, serve
    )
    assert stop.is_set()
    assert work_items.deferred == ["workitem:messaging-intake:telegram"]


def test_one_platform_loss_leaves_the_other_serving():
    work_items = FakeWorkItems(renew=lambda item: not item.endswith("mattermost"))
    stop = threading.Event()

    def serve(platforms, stop_event, platform_events):
        assert platforms == ["mattermost", "telegram"]
        assert platform_events["mattermost"].wait(timeout=2.0)
        assert not platform_events["telegram"].is_set()
        assert not stop_event.is_set()
        stop_event.set()

    intake_lease.run_with_intake_leases(
        _engine(work_items), (_lease("mattermost"), _lease("telegram")), stop, serve
    )


def test_renewal_thread_keeps_the_callers_context():
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="")
    seen: list[str] = []
    renewed = threading.Event()

    def renew_answer(_item: str) -> bool:
        seen.append(marker.get())
        renewed.set()
        return True

    stop = threading.Event()

    def serve(platforms, stop_event, platform_events):
        assert renewed.wait(timeout=2.0)
        stop_event.set()

    marker.set("caller")
    intake_lease.run_with_intake_leases(
        _engine(FakeWorkItems(renew=renew_answer)), (_lease("telegram"),), stop, serve
    )
    assert seen and set(seen) == {"caller"}
