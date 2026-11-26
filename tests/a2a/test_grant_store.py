"""The human-grant store: consume once, refuse expired and revoked grants."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arbora.a2a.approval import PendingCallBinding
from arbora.a2a.grant_store import GrantError, GrantRefusal, HumanGrantStore
from arbora.api.invoke.plan import PLAN_TTL_MS, PlanBinding, params_digest

pytestmark = pytest.mark.spec("ARBORA-A2A-R005.2.1")

KIND = "arbora.human_grant"
NOW = 1_800_000_000_000


class Leases:
    """The engine's control-lease semantics: a revision compare-and-set per row.

    Reads yield to the loop so concurrent callers all see the same revision; the
    transition is atomic, as the engine's write transaction is.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.consumptions: list[Any] = []
        self.down = False

    async def issue(self, **fields: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        if self.down:
            raise ConnectionError("engine unreachable")
        if fields["lease_id"] in self.rows:
            return {"outcome": "collision"}
        self.rows[fields["lease_id"]] = {
            "lease_id": fields["lease_id"],
            "tenant": fields["tenant"],
            "kind": fields["kind"],
            "grant": fields["grant"],
            "status": "active",
            "revision": 1,
            "hard_expires_at_ms": fields["hard_expires_at_ms"],
        }
        return {"outcome": "issued"}

    async def get(self, *, tenant: str, lease_id: str) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        if self.down:
            raise ConnectionError("engine unreachable")
        row = self.rows.get(lease_id)
        return dict(row) if row and row["tenant"] == tenant else None

    async def transition(
        self,
        *,
        tenant: str,
        lease_id: str,
        expected_revision: int,
        to: str,
        idempotency_key: str,
        pending_call_consumption: Any = None,
    ) -> dict[str, Any]:
        row = self.rows[lease_id]
        if row["tenant"] != tenant:
            return {"outcome": "not_found", "lease": None}
        if row["revision"] != expected_revision or row["status"] != "active":
            return {"outcome": "conflict", "lease": dict(row)}
        row.update(status=to, revision=row["revision"] + 1)
        if pending_call_consumption is not None:
            self.consumptions.append(pending_call_consumption)
        return {"outcome": "applied", "lease": dict(row)}


class Client:
    def __init__(self) -> None:
        self.control_leases = Leases()


def binding(grant_id: str = "grant:one", *, tenant: str = "tenant:one") -> Any:
    plan = PlanBinding(
        "tool.write",
        params_digest({"value": 1}),
        "person:one",
        tenant,
        "policy:one",
        "a" * 64,
        "write",
        "plan",
    )
    return PendingCallBinding(
        task_id="task:one",
        parent_work_item_id="work:parent",
        child_work_item_id="work:child",
        pending_call_id="call:one",
        plan_ref="plan:one",
        plan=plan,
        approving_human="person:one",
        grant_id=grant_id,
        grant_revision=1,
        issued_at_ms=NOW,
        expires_at_ms=NOW + PLAN_TTL_MS,
    )


def store(client: Client, clock: list[int]) -> HumanGrantStore:
    return HumanGrantStore(client, lease_kind=KIND, clock_ms=lambda: clock[0])


def test_concurrent_consume_succeeds_once() -> None:
    client, clock = Client(), [NOW + 1]
    grants = store(client, clock)

    async def run() -> list[Any]:
        await grants.issue(binding(), registration={"work_item": "pair"})
        return await asyncio.gather(
            *(
                grants.consume("tenant:one", "grant:one", consumption={"pair": "x"})
                for _ in range(8)
            ),
            return_exceptions=True,
        )

    results = asyncio.run(run())
    winners = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, GrantError)]
    assert len(winners) == 1 and len(refused) == 7
    assert {r.code for r in refused} == {GrantRefusal.REPLAYED}
    assert winners[0].grant_id == "grant:one" and winners[0].pending_call_id == (
        "call:one"
    )
    assert client.control_leases.consumptions == [{"pair": "x"}]
    assert client.control_leases.rows["grant:one"]["status"] == "consumed"


def test_expired_and_revoked_grants_refuse() -> None:
    client, clock = Client(), [NOW + 1]
    grants = store(client, clock)

    async def code(call: Any) -> GrantRefusal:
        with pytest.raises(GrantError) as caught:
            await call
        return caught.value.code

    async def run() -> None:
        await grants.issue(binding("grant:expiring"))
        clock[0] = NOW + PLAN_TTL_MS
        assert (
            await code(grants.consume("tenant:one", "grant:expiring"))
            == GrantRefusal.EXPIRED
        )
        clock[0] = NOW + 1
        await grants.issue(binding("grant:revoked"))
        evidence = await grants.revoke("tenant:one", "grant:revoked")
        assert evidence.outcome == "revoked"
        assert (
            await code(grants.consume("tenant:one", "grant:revoked"))
            == GrantRefusal.REVOKED
        )
        # A foreign tenant's grant answers exactly like an absent one.
        assert (
            await code(grants.consume("tenant:two", "grant:revoked"))
            == await code(grants.consume("tenant:one", "grant:never-issued"))
            == GrantRefusal.EXPIRED
        )
        # A reused grant id never overwrites the first grant.
        assert (
            await code(grants.issue(binding("grant:revoked"))) == GrantRefusal.REPLAYED
        )

    asyncio.run(run())


def test_an_unreachable_engine_grants_nothing() -> None:
    client, clock = Client(), [NOW + 1]
    grants = store(client, clock)

    async def run() -> None:
        await grants.issue(binding())
        client.control_leases.down = True
        with pytest.raises(GrantError) as caught:
            await grants.consume("tenant:one", "grant:one")
        assert caught.value.code == GrantRefusal.STORE_UNAVAILABLE
        client.control_leases.down = False
        assert client.control_leases.rows["grant:one"]["status"] == "active"

    asyncio.run(run())


def test_grant_lifetime_never_exceeds_the_plan_lifetime() -> None:
    client, clock = Client(), [NOW - 100_000]
    grants = store(client, clock)
    seen: list[int] = []
    original = client.control_leases.issue

    async def spy(**fields: Any) -> Any:
        seen.append(fields["hard_expires_at_ms"] - fields["issued_at_ms"])
        return await original(**fields)

    client.control_leases.issue = spy
    asyncio.run(grants.issue(binding()))
    assert seen == [PLAN_TTL_MS]
