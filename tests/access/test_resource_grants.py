"""Resource access models and evaluation (ARBORA-IDENTITY-R019.1, R019.2)."""

from __future__ import annotations

import pytest

from arbora.access.resource_grants import (
    ResourceAccessRequest,
    ResourceGrant,
    ResourceRefusal,
    evaluate,
    recheck_seconds,
)

REQUEST = {
    "tenant": "t1",
    "principal": "ada",
    "client": "cli",
    "resource_ref": "res:db1",
    "protocol": "database",
    "operation": "query",
}
GRANT = {
    "engine_decision_ref": "g1",
    "subject": "user:ada",
    "tenant": "t1",
    "resource_ref": "res:db1",
    "resource_version": "3",
    "protocol": "database",
    "operation": "query",
    "scope_ceiling": ["sql:read"],
    "expires_at": 10_000,
    "revocation_revision": 7,
}


@pytest.mark.spec("ARBORA-IDENTITY-R019.1")
def test_models_round_trip_and_refuse_unknown_protocol() -> None:
    request = ResourceAccessRequest(**REQUEST, constraints={"b": "2", "a": "1"})
    assert request.constraints == (
        ("a", "1"),
        ("b", "2"),
    )  # an immutable tuple of pairs
    assert ResourceAccessRequest(**{**REQUEST, "protocol": "web"}).protocol == "web"
    for bad in ({"protocol": "telnet"}, {"resource_ref": ""}, {"operation": " "}):
        with pytest.raises(ValueError, match="RESOURCE_REQUEST_INVALID"):
            ResourceAccessRequest(**{**REQUEST, **bad})
    grant = ResourceGrant(**GRANT)
    assert grant.scope_ceiling == ("sql:read",)
    with pytest.raises(ValueError, match="RESOURCE_REQUEST_INVALID"):
        ResourceGrant(**{**GRANT, "protocol": "gopher"})
    with pytest.raises(AttributeError):
        request.protocol = "web"  # type: ignore[misc]


@pytest.mark.spec("ARBORA-IDENTITY-R019.2")
def test_evaluate_matches_all_dimensions_and_expires() -> None:
    request = ResourceAccessRequest(**REQUEST)
    grant = ResourceGrant(**GRANT)

    def check(
        grant: ResourceGrant | None = grant, now: int = 5_000, rev: int = 7, **kw
    ):
        return evaluate(
            request, grant, now, rev, resource_version=kw.pop("v", "3"), **kw
        )

    assert check().allowed
    assert check(None).refusal is ResourceRefusal.GRANT_MISSING
    assert check(now=10_000).refusal is ResourceRefusal.GRANT_EXPIRED
    assert check(rev=8).refusal is ResourceRefusal.POLICY_STALE
    assert check(v="4").refusal is ResourceRefusal.UNKNOWN
    other = {**GRANT, "tenant": "t2"}
    assert check(ResourceGrant(**other)).refusal is ResourceRefusal.TENANT_MISMATCH
    for change in (
        {"subject": "user:bob"},
        {"protocol": "web"},
        {"operation": "write"},
    ):
        assert not check(ResourceGrant(**{**GRANT, **change})).allowed
    assert check(ResourceGrant(**{**GRANT, "resource_ref": "res:db2"})).refusal is (
        ResourceRefusal.UNKNOWN
    )
    role = ResourceGrant(**{**GRANT, "subject": "role:dba"})
    assert check(role).refusal is ResourceRefusal.ROLE_UNTRUSTED
    assert check(role, trusted_roles=("dba",)).allowed
    assert recheck_seconds() == 5 and recheck_seconds(60) == 60
    for bad in (0, 61, "5", True):
        with pytest.raises(ValueError):
            recheck_seconds(bad)
