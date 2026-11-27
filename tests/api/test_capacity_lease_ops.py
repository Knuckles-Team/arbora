"""The six served build-slot lease operations, over the served app (ARBORA-CAPACITY-R754.1, .2).

The engine is a recorded double. Its replies and refusals are the vectors the engine side
asserts (``tests/api/vectors/capacity_lease_wire.json``, recorded from the engine's own
wire vectors with the holder text replaced). Arbora holds no lease state: every answer is
the engine's, and every engine request is built from the verified caller.
"""

# ruff: noqa: F811

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arbora.api.contracts.provisioning import opaque_principal
from arbora.api.errors import EngineRefusal
from arbora.api.ops import capacity_lease, pin_guards
from arbora.api.ops.engine_calls import refusal_code
from arbora.api.registry import Confirm, Registry
from tests.api._loopback_engine import Loopback, loopback  # noqa: E402,F401
from tests.test_served_operation_api import served  # noqa: E402,F401

VECTORS = json.loads(
    (Path(__file__).parent / "vectors" / "capacity_lease_wire.json").read_text()
)
HOLDER = "principal:sha256:HOLDER"
SCOPES = ("capacity:lease", "capacity:read", "capacity:admin")
OPS = {
    "AcquireCapacity": "capacity.lease.request",
    "StartCapacity": "capacity.lease.start",
    "RenewCapacity": "capacity.lease.renew",
    "ReleaseCapacity": "capacity.lease.release",
    "RevokeCapacity": "capacity.lease.revoke",
    "CapacityStatus": "capacity.lease.get",
}


def _params(vector: dict[str, Any]) -> dict[str, Any]:
    """The served params a recorded engine request corresponds to."""
    sent = vector["request"]["params"]["request"]
    name = vector["name"]
    if name == "AcquireCapacity":
        return {
            "cell_id": sent["demands"][0]["cell_id"],
            "priority": sent["priority"],
            "work_item_id": sent["work_item_id"],
            "ttl_ms": sent["ttl_ms"],
        }
    if name in {"StartCapacity", "RenewCapacity", "ReleaseCapacity"}:
        return {**sent["leases"][0], "ttl_ms": sent["ttl_ms"]}
    if name == "RevokeCapacity":
        return {"lease_id": sent["lease_id"], "reason": sent["reason"]}
    return {"lease_id": sent["lease_id"]}


def _serve(loopback: Loopback, answers: dict[str, Any]) -> None:
    """Serve the engine methods: a value is the recorded reply, an error is a refusal."""

    def answer(method: str) -> Any:
        def handler(params: dict[str, Any], _principal: str) -> Any:
            reply = answers[method]
            if isinstance(reply, EngineRefusal):
                raise reply
            return reply

        return handler

    loopback.serve_unreleased(pin_guards, {m: answer(m) for m in answers})
    loopback.serve({}, *SCOPES)


def _call(loopback: Loopback, vector: dict[str, Any]) -> Any:
    op = OPS[vector["name"]]
    params, key = _params(vector), "key-" + vector["name"].lower()
    if vector["name"] == "RevokeCapacity":
        return loopback.confirmed(op, params, key)
    return loopback.call(op, params, key)


def _replay_methods(loopback: Loopback, answers: dict[str, Any]) -> None:
    """Each recorded reply reaches the caller with the engine's lease unchanged."""
    _serve(loopback, answers)
    for vector in VECTORS["methods"]:
        if vector["reply"] is not answers[vector["name"]]:
            continue  # the queued acquire is replayed separately
        loopback.engine_calls.clear()
        reply = _call(loopback, vector)
        assert reply.status_code == 200, reply.text
        value = Loopback.value(reply)
        engine_method, sent, _ = loopback.engine_calls[-1]
        assert engine_method == vector["name"]
        # The wire shape the engine's own vector records: no field added or dropped.
        assert set(sent["request"]) == set(vector["request"]["params"]["request"])
        assert value["lease"]["lease_id"] == "lease-lane-1"
        if vector["name"] != "CapacityStatus":
            expected = answers[vector["name"]]["leases"][0]["state"]
            assert value["lease"]["state"] == expected


def _replay_refusals(loopback: Loopback) -> None:
    """Each emitted engine refusal reaches the caller with its own code."""
    for refusal in VECTORS["refusals"]:
        if refusal.get("emitted") is False or refusal["code"] == "ACCESS_DENIED":
            continue  # not emitted, or refused before the engine (R754.2)
        method = refusal["method"]
        vector = next(v for v in VECTORS["methods"] if v["name"] == method)
        code = refusal_code(refusal["code"], refusal["error"])
        _serve(loopback, {method: EngineRefusal(code)})
        reply = _call(loopback, vector)
        assert reply.json()["error"]["code"] == refusal["code"], reply.text


@pytest.mark.spec("ARBORA-CAPACITY-R754.1")
def test_six_lease_operations_are_registered_and_pass_engine_refusals_through(
    loopback: Loopback,
) -> None:
    registry = Registry(capacity_lease.operations())
    assert sorted(op.id for op in registry) == sorted(OPS.values())
    assert registry["capacity.lease.request"].scopes == {"capacity:lease"}
    assert registry["capacity.lease.get"].scopes == {"capacity:read"}
    assert registry["capacity.lease.revoke"].scopes == {"capacity:admin"}
    assert registry["capacity.lease.revoke"].confirm is Confirm.PLAN
    from arbora.api.ops.registry_factory import get_registry

    assert set(OPS.values()) <= {op.id for op in get_registry()}

    answers = {v["name"]: v["reply"] for v in VECTORS["methods"][::-1]}
    _replay_methods(loopback, answers)
    queued = VECTORS["methods"][1]
    _serve(loopback, {"AcquireCapacity": queued["reply"]})
    value = Loopback.value(_call(loopback, queued))
    assert (value["lease"]["state"], value["queue_position"]) == ("queued", 1)
    _replay_refusals(loopback)


@pytest.mark.spec("ARBORA-CAPACITY-R754.2")
def test_holder_digest_comes_from_the_verified_caller(loopback: Loopback) -> None:
    renew = next(v for v in VECTORS["methods"] if v["name"] == "RenewCapacity")
    _serve(loopback, {"RenewCapacity": renew["reply"]})
    served_params = _params(renew)

    # A request that names a holder, a tenant or a digest is refused as an unknown field.
    for field in ("actor_digest", "owner_digest", "tenant_ref"):
        forged = loopback.call(
            "capacity.lease.renew", {**served_params, field: HOLDER}, "key-forged"
        )
        assert forged.status_code == 400, forged.text
    assert loopback.engine_calls == []

    # The engine request carries the digest of the verified caller and its tenant.
    ok = loopback.call("capacity.lease.renew", served_params, "key-owner")
    assert ok.status_code == 200, ok.text
    _, sent, principal = loopback.engine_calls[-1]
    assert sent["request"]["owner_digest"] == opaque_principal(principal)
    assert sent["request"]["tenant_ref"]

    # Renewing another holder's lease is the engine's own refusal, passed through.
    refused = next(r for r in VECTORS["refusals"] if r.get("reason") == "guard_failed")
    _serve(loopback, {"ReleaseCapacity": EngineRefusal(refused["code"])})
    release = next(v for v in VECTORS["methods"] if v["name"] == "ReleaseCapacity")
    reply = _call(loopback, release)
    assert reply.json()["error"]["code"] == "TRANSITION_REFUSED"

    # A caller without the scope is refused before any engine call.
    loopback.engine_calls.clear()
    loopback.serve({})
    denied = loopback.call("capacity.lease.renew", served_params, "key-denied")
    assert denied.status_code in {401, 403}, denied.text
    assert loopback.engine_calls == []
