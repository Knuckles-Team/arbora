"""Fleet acceptance: one passing and one stale connector block the receipt (ARBORA-RELEASE-R003.9)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.steps import OpResult
from arbora.api.ops import fleet as fleet_ops
from arbora.fleet import acceptance

GOOD_DIGEST = "a" * 64
STALE_DIGEST = "b" * 64


class Gateway:
    """``fleet.status`` of two running children, one reporting a different tool count."""

    async def status(self, caller: Any, *, servers: tuple[str, ...]) -> Any:
        rows = {
            "alpha": {"state": "up", "breaker": "closed", "mounted_tools": 3},
            "beta": {"state": "up", "breaker": "closed", "mounted_tools": 5},
        }
        return {"children": {name: rows[name] for name in servers}}


class Onboarding:
    """The connector's own pack digest equals the engine's only for ``alpha``."""

    async def pack_unchanged(self, endpoint: Any, pack: Any) -> bool:
        return endpoint.name == "alpha"


async def _pack_status(context: Any, params: dict[str, Any], op: Any) -> dict[str, Any]:
    name = params["connector"]
    return {
        "value": {
            "head": {"pack_digest": GOOD_DIGEST if name == "alpha" else STALE_DIGEST},
            "members": {"published": 3, "retired": 0, "withdrawn": 0},
        }
    }


async def _invoke(op: str, params: dict[str, Any], caller: Any, key: str) -> Any:
    assert op == "fleet.call" and key.startswith("acceptance:")
    return OpResult({"value": "ok"})


def _context() -> Any:
    return SimpleNamespace(
        caller=SimpleNamespace(tenant="tenant:one"),
        services={
            "fleet_gateway": Gateway(),
            "fleet_onboarding": Onboarding(),
            "plan_step_invoke": _invoke,
        },
    )


@pytest.mark.spec("ARBORA-RELEASE-R003.9")
def test_two_connector_fixture_gives_one_pass_one_fail_and_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.fleet import acceptance_ports, onboarding

    endpoints = tuple(
        onboarding.FleetEndpoint(name=name, url=f"http://127.0.0.1/{name}")
        for name in ("alpha", "beta")
    )
    monkeypatch.setattr(onboarding, "configured_fleet_endpoints", lambda **_: endpoints)

    async def capture(endpoint: Any, *, auth: Any = None) -> Any:
        return object()

    monkeypatch.setattr(onboarding, "capture_server_pack", capture)
    monkeypatch.setattr(fleet_ops, "handle_pack_status", _pack_status)
    probes = {
        "alpha": {"tool": "list_things", "arguments": {}},
        "beta": {"tool": "list_things", "arguments": {}},
    }
    op = SimpleNamespace(id="fleet.acceptance.check")

    reply = asyncio.run(
        fleet_ops.handle_fleet_acceptance(
            _context(),
            {"connectors": (), "expected_skip": {}, "probes": probes},
            op,
        )
    )
    receipt = reply["value"]
    rows = {row["connector"]: row for row in receipt["connectors"]}
    assert set(rows["alpha"]["results"].values()) == {acceptance.CHECK_OK}
    assert rows["beta"]["results"]["tool_count"] == acceptance.FAIL
    assert rows["beta"]["results"]["pack_digest"] == acceptance.FAIL
    assert rows["beta"]["results"]["health"] == acceptance.CHECK_OK
    assert receipt["state"] == "blocked"
    assert receipt["refusal"] == "ACCEPTANCE_BLOCKED"
    assert acceptance_ports.ContextAcceptancePorts is not None


@pytest.mark.spec("ARBORA-RELEASE-R003.9")
def test_gap_never_passes_and_a_skip_needs_a_reason() -> None:
    class Silent:
        async def child(self, connector: str) -> None:
            return None

        async def pack(self, connector: str) -> None:
            return None

        async def read_call(self, connector: str) -> None:
            return None

        async def digest_matches(self, connector: str) -> None:
            return None

    receipt = asyncio.run(acceptance.check_fleet(Silent(), ["gamma"]))
    assert receipt["state"] == "blocked"
    assert set(receipt["connectors"][0]["results"].values()) == {acceptance.GAP}

    skipped = asyncio.run(
        acceptance.check_fleet(Silent(), ["gamma"], {"gamma": "needs a licence"})
    )
    assert skipped["state"] == "passed"
    assert set(skipped["connectors"][0]["results"].values()) == {acceptance.SKIP}

    unreasoned = asyncio.run(
        acceptance.check_fleet(Silent(), ["gamma"], {"gamma": " "})
    )
    assert unreasoned["state"] == "blocked"
