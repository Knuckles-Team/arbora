"""The A2A persistence doctor check (ARBORA-HOST-R026.4)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.deployment.doctor_a2a import (
    A2A_STORE_NOT_DURABLE,
    A2A_STORE_UNBOUND,
    PROBE_TENANT,
    check_a2a_persistence,
)

TASK_ID = "a2a-" + "a" * 64


class Authority:
    """Recorded stand-in for the engine-backed task authority."""

    def __init__(self, *, fail_dispatch: bool = False) -> None:
        self.dispatched: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self._fail = fail_dispatch

    async def dispatch(self, **kwargs: Any) -> Any:
        if self._fail:
            raise RuntimeError("engine down")
        self.dispatched.append(kwargs)
        return SimpleNamespace(id=TASK_ID)

    async def cancel(self, task_id: str) -> Any:
        self.cancelled.append(task_id)


def _service(authority: Authority) -> Any:
    return SimpleNamespace(authority=authority)


@pytest.mark.spec("ARBORA-HOST-R026.4")
async def test_a2a_persistence_pass_and_both_failures() -> None:
    # pass: the engine returns the probe task, which is then cancelled.
    authority = Authority()

    async def engine_read(task_id: str) -> Any:
        return SimpleNamespace(id=task_id)

    passed = await check_a2a_persistence(_service(authority), engine_read)
    assert passed["status"] == "ok"
    assert authority.cancelled == [TASK_ID]
    sent = authority.dispatched[0]
    assert sent["message"].metadata["tenant"] == PROBE_TENANT
    assert sent["blocking"] is False

    # fail: no engine client is bound.
    unbound = await check_a2a_persistence(None, None)
    assert unbound["status"] == "fail"
    assert A2A_STORE_UNBOUND in unbound["detail"]
    assert unbound["data"]["code"] == A2A_STORE_UNBOUND

    # fail: the read-back differs, and the probe task is still cancelled.
    lossy = Authority()

    async def engine_forgets(_task_id: str) -> Any:
        return None

    differing = await check_a2a_persistence(_service(lossy), engine_forgets)
    assert differing["status"] == "fail"
    assert differing["data"]["code"] == A2A_STORE_NOT_DURABLE
    assert lossy.cancelled == [TASK_ID]


@pytest.mark.spec("ARBORA-HOST-R026.4")
async def test_a_failed_dispatch_reports_not_durable_and_cancels_nothing() -> None:
    authority = Authority(fail_dispatch=True)

    async def never(_task_id: str) -> Any:
        raise AssertionError("no read-back without a created task")

    result = await check_a2a_persistence(_service(authority), never)
    assert result["status"] == "fail"
    assert result["data"]["code"] == A2A_STORE_NOT_DURABLE
    assert authority.cancelled == []


@pytest.mark.spec("ARBORA-HOST-R026.4")
def test_the_doctor_registers_the_check_and_skips_outside_a_serving_process() -> None:
    from arbora.deployment import doctor

    assert "a2a_persistence" in doctor._LIVE_CHECK_NAMES
    assert doctor.CHECKS["a2a_persistence"] is doctor._check_a2a_persistence
    assert doctor._check_a2a_persistence(live=False)["status"] == "skip"
    assert doctor._check_a2a_persistence(live=True)["status"] == "skip"
