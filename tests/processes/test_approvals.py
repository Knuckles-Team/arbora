"""The approval broker decides one pending wait once and journals its evidence (R898.4, R898.7)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from arbora.processes.approvals import (
    ApprovalBroker,
    ApprovalNotPending,
    ApproverRoleRequired,
)


class _Journal:
    """The engine key-value journal: versioned values per ``(namespace, key)``."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[int, str]] = {}
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def __call__(
        self, method: str, params: dict[str, Any], key: str | None
    ) -> Any:
        self.calls.append((method, params, key))
        slot = (params["namespace"], params.get("key", ""))
        if method == "KvGet":
            version, value = self.rows.get(slot, (0, ""))
            return {"value": value, "version": version} if version else None
        if method == "KvCas":
            version = self.rows.get(slot, (0, ""))[0]
            if version != params["expected_version"]:
                error = RuntimeError("version mismatch")
                error.code = "CAS_CONFLICT"  # type: ignore[attr-defined]
                raise error
            self.rows[slot] = (version + 1, params["new"])
            return {"version": version + 1}
        entries = [
            {"value": value, "version": version}
            for (ns, k), (version, value) in sorted(self.rows.items())
            if ns == params["namespace"] and k.startswith(params["prefix"])
        ]
        return {"entries": entries}


def _broker() -> tuple[ApprovalBroker, _Journal]:
    journal = _Journal()
    return ApprovalBroker(journal), journal


async def _wait(broker: ApprovalBroker, tenant: str = "tenant:one") -> None:
    await broker.wait(
        tenant,
        run_id="run:1",
        step_id="s1",
        approver_role="role:finance",
        requested_ms=5,
    )


@pytest.mark.spec("ARBORA-OPS-R898.4")
async def test_decide_resolves_one_pending_wait_once() -> None:
    broker, _journal = _broker()
    await _wait(broker)
    await _wait(broker)  # a repeated wait finds the same record
    assert [r["state"] for r in await broker.records("tenant:one", "run:1")] == [
        "pending"
    ]
    decided = await broker.resolve(
        "tenant:one",
        run_id="run:1",
        step_id="s1",
        verdict="approve",
        comment="ok",
        principal="person:one",
        groups=("role:finance",),
        decided_ms=9,
    )
    assert decided["state"] == "decided" and decided["approver"] == "person:one"
    with pytest.raises(ApprovalNotPending):  # a second decision finds nothing pending
        await broker.resolve(
            "tenant:one",
            run_id="run:1",
            step_id="s1",
            verdict="reject",
            comment="",
            principal="person:two",
            groups=("role:finance",),
            decided_ms=10,
        )


@pytest.mark.spec("ARBORA-OPS-R898.4")
async def test_an_approver_without_the_role_and_a_foreign_tenant_decide_nothing() -> (
    None
):
    broker, _journal = _broker()
    await _wait(broker)
    with pytest.raises(ApproverRoleRequired):
        await broker.resolve(
            "tenant:one",
            run_id="run:1",
            step_id="s1",
            verdict="approve",
            comment="",
            principal="person:one",
            groups=("role:other",),
            decided_ms=9,
        )
    with pytest.raises(
        ApprovalNotPending
    ):  # another tenant's wait is the same as absent
        await broker.resolve(
            "tenant:two",
            run_id="run:1",
            step_id="s1",
            verdict="approve",
            comment="",
            principal="person:x",
            groups=("role:finance",),
            decided_ms=9,
        )
    assert await broker.records("tenant:two", "run:1") == []
    with pytest.raises(ValueError):
        await broker.resolve(
            "tenant:one",
            run_id="run:1",
            step_id="s1",
            verdict="maybe",
            comment="",
            principal="p",
            groups=("role:finance",),
            decided_ms=1,
        )


@pytest.mark.spec("ARBORA-OPS-R898.7")
async def test_journal_cites_plan_verdicts_and_approver() -> None:
    broker, journal = _broker()
    await broker.journal_run(
        "tenant:one",
        run_id="run:1",
        plan_digest="sha256:plan",
        workflow_id="wf",
        version=2,
        binding_sources={"s2": "graph", "s1": "relevance"},
        branch_verdicts=["rec:branch"],
        decision_records=["rec:table"],
    )
    await _wait(broker)
    await broker.resolve(
        "tenant:one",
        run_id="run:1",
        step_id="s1",
        verdict="reject",
        comment="no",
        principal="person:one",
        groups=("role:finance",),
        decided_ms=42,
    )
    evidence = await broker.evidence("tenant:one", "run:1")
    assert evidence["run"]["plan_digest"] == "sha256:plan"
    assert (
        evidence["run"]["workflow_id"] == "wf"
        and evidence["run"]["workflow_version"] == 2
    )
    assert evidence["run"]["binding_sources"] == {"s1": "relevance", "s2": "graph"}
    assert evidence["run"]["branch_verdicts"] == ["rec:branch"]
    assert evidence["run"]["decision_records"] == ["rec:table"]
    (approval,) = evidence["approvals"]
    assert (approval["approver"], approval["decided_ms"], approval["verdict"]) == (
        "person:one",
        42,
        "reject",
    )
    # Every record is a stored engine journal row, never process-local state.
    assert all(
        json.loads(v)["tenant"] == "tenant:one" for _, v in journal.rows.values()
    )
